import os
import copy
import escapism
import string
import json
import jupyterhub.services.service
import kubespawner
from traitlets import Integer, Unicode
class _KubeServiceSpawner(kubespawner.KubeSpawner):
    """ Similar to _ServiceSpawner in JupyterHub, but using KubeSpawner.

    Removes notebook-specific-ness from KubeSpawner (TODO: verify this is true).
    """

    name = Unicode(config=True, help="Name of service")
    service_url = Unicode(config=True, help="Service URL")
    service_prefix = Unicode(config=True, help="Service prefix")
    # TODO: These three configs are just tacked on here.
    # Find a better way to pass those in.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    def _expand_user_properties(self, template):
        # Make sure username and servername match the restrictions for DNS labels
        # Note: '-' is not in safe_chars, as it is being used as escape character
        safe_chars = set(string.ascii_lowercase + string.digits)

        # Set servername based on whether named-server initialised
        if self.name:
            servername = '-{}'.format(self.name)
            safe_servername = '-{}'.format(escapism.escape(self.name, safe=safe_chars, escape_char='-').lower())
        else:
            servername = ''
            safe_servername = ''

        return template.format(
            servername=safe_servername,
            unescaped_servername=servername
        )
    def _build_common_annotations(self, extra_annotations):
        # Annotations don't need to be escaped
        annotations = {
            'hub.jupyter.org/servicename': self.name
        }
        if self.name:
            annotations['hub.jupyter.org/servername'] = self.name

        annotations.update(extra_annotations)
        return annotations
    def get_env(self):
        env={}
        for key in self.env_keep:
            if key in os.environ:
                env[key] = os.environ[key]
        for key, value in self.environment.items():
            if callable(value):
                env[key] = value(self)
            else:
                env[key] = value

        env['JUPYTERHUB_API_TOKEN'] = self.api_token
        # deprecated (as of 0.7.2), for old versions of singleuser
        env['JPY_API_TOKEN'] = self.api_token
        if self.admin_access:
            env['JUPYTERHUB_ADMIN_ACCESS'] = '1'
        # OAuth settings
        env['JUPYTERHUB_CLIENT_ID'] = self.oauth_client_id
        if self.cookie_options:
            env['JUPYTERHUB_COOKIE_OPTIONS'] = json.dumps(self.cookie_options)
        env['JUPYTERHUB_HOST'] = self.hub.public_host

        env['JUPYTERHUB_SERVER_NAME'] = self.name
        env['JUPYTERHUB_API_URL'] = self.hub.api_url

        env['JUPYTERHUB_BASE_URL'] = self.hub.base_url[:-4]

        env['JUPYTERHUB_SERVICE_NAME'] = self.name
        env['JUPYTERHUB_SERVICE_URL'] = self.service_url
        env['JUPYTERHUB_SERVICE_PREFIX'] = self.service_prefix

        env['JUPYTER_IMAGE_SPEC'] = self.image
        env['JUPYTER_IMAGE'] = self.image

        return env

class Service(jupyterhub.services.service.Service):
    """An object wrapping a service specification for Hub API consumers.

    Inherited from the JupyterHub "service" class, so it supports all the standard inputs:
    name, admin, url, oauth_no_confirm, command, environment, user

    In addition, we have the following configs:
    - image: str
        Specify the Docker image to run when spawning the service.
        If unset, this is not a kubespawner service, and the regular JupyterHub behavior will occur.
    - port: int
        Specify the port on which the service in the pod listens to.
        The url config will be ignored and replaced by the pod IP and the port.
        If unset, a random port will be selected.

    TODO: Add more configs to fine-tune kubespawner.
    """

    image = Unicode(help="""The Docker image to run when spawning a Kubernetes-managed service.""").tag(
        input=True
    )

    port = Integer(help="""The port on which the Kubernetes service will run.""").tag(
        input=True
    )

    @property
    def kube_managed(self):
        """Am I a Kubernetes pod managed by the Hub?"""
        return bool(self.image)

    @property
    def subprocess_managed(self):
        """Am I a subprocess managed by the Hub?"""
        return bool(self.command)

    @property
    def managed(self):
        """Am I managed by the Hub?"""
        return self.kube_managed or self.subprocess_managed

    @property
    def kind(self):
        """The name of the kind of service as a string

        - 'managed' for managed services
        - 'external' for external services
        """
        return 'kube managed' if self.kube_managed else 'subprocess managed' if self.subprocess_managed else 'external'

    @property
    def proxy_spec(self):
        if not self.server:
            return ''
        if not self.kube_managed and self.domain:
            return self.domain + self.server.base_url
        else:
            return self.server.base_url

    async def start(self):
        """Start a managed service"""
        if not self.kube_managed:
            return jupyterhub.services.service.Service.start(self)
        self.log.info("Starting Kubernetes pod for service %r: %r", self.name, self.image)

        if self.port:
            self.url = "http://0.0.0.0:%d/" % self.port

        hub = copy.deepcopy(self.hub)
        hub.connect_url = ''
        hub.connect_ip = os.environ['HUB_SERVICE_HOST']

        self.spawner = _KubeServiceSpawner(
            name=self.name,
            cmd=self.command,
            #environment=env,
            api_token=self.api_token,
            #oauth_client_id=self.oauth_client_id,
            #cookie_options=self.cookie_options,
            #cwd=None,
            hub=self.hub,
            pod_name_template="jupyter-service{servername}",
            storage_pvc_ensure=True,
            storage_capacity='1G',
            pvc_name_template="claim-service{servername}",
            image=self.image,
            port=self.port,
            volumes=[{
                'name': "volume-service{servername}",
                'persistentVolumeClaim': {
                    'claimName': "claim-service{servername}"
                }
            }],
            volume_mounts=[{
                'name': "volume-service{servername}",
                'mountPath': '/srv/%s'%self.name
            }],
            service_url=self.url,
            service_prefix=self.server.base_url,
        )
        self.domain, _ =  await self.spawner.start()
        # Can't understand the observer logic that supposedly autoupdates the database
        # I have to do this otherwise the proxy will use the wrong IP
        self.server.ip = self.domain
        self.server.orm_server.ip = self.domain
        self.db.add(self.server.orm_server)
        self.db.commit()
        # TODO polling?

    async def stop(self):
        """Stop a managed service"""
        if not self.kube_managed:
            return jupyterhub.services.service.Service.start(self)
        self.log.debug("Stopping service %s", self.name)
        if self.spawner:
            if self.orm.server:
                self.db.delete(self.orm.server)
                self.db.commit()
            return await self.spawner.stop()

