import jupyterhub.app
import tornado.httpserver
import os
import atexit
from urllib.parse import urlparse, unquote
from tornado.ioloop import IOLoop, PeriodicCallback
from jupyterhub import orm
from jupyterhub.objects import Server
from jupyterhub.utils import make_ssl_context
from .service import Service

class JupyterHub(jupyterhub.app.JupyterHub):
    def init_services(self):
        """Override JupyterHub's default init_services class to support initializing Kubernetes services"""

        self._service_map.clear()
        if self.domain:
            domain = 'services.' + self.domain
            parsed = urlparse(self.subdomain_host)
            host = '%s://services.%s' % (parsed.scheme, parsed.netloc)
        else:
            domain = host = ''

        for spec in self.services:
            self.log.info("Reach app.py kubespawner services %s"%spec)
            if 'name' not in spec:
                raise ValueError('service spec must have a name: %r' % spec)
            name = spec['name']
            # get/create orm
            orm_service = orm.Service.find(self.db, name=name)
            if orm_service is None:
                # not found, create a new one
                orm_service = orm.Service(name=name)
                self.db.add(orm_service)
            orm_service.admin = spec.get('admin', False)
            self.db.commit()
            service = Service(
                parent=self,
                app=self,
                base_url=self.base_url,
                db=self.db,
                orm=orm_service,
                domain=domain,
                host=host,
                hub=self.hub,
            )

            traits = service.traits(input=True)
            for key, value in spec.items():
                if key not in traits:
                    raise AttributeError("No such service field: %s" % key)
                setattr(service, key, value)

            if service.managed:
                if not service.api_token:
                    # generate new token
                    # TODO: revoke old tokens?
                    service.api_token = service.orm.new_api_token(
                        note="generated at startup"
                    )
                else:
                    # ensure provided token is registered
                    self.service_tokens[service.api_token] = service.name
            else:
                self.service_tokens[service.api_token] = service.name

            if service.image and service.port:
                service.url = "http://0.0.0.0:%d/"%service.port

            if service.url:
                parsed = urlparse(service.url)
                if parsed.port is not None:
                    port = parsed.port
                elif parsed.scheme == 'http':
                    port = 80
                elif parsed.scheme == 'https':
                    port = 443
                server = service.orm.server = orm.Server(
                    proto=parsed.scheme,
                    ip=parsed.hostname,
                    port=port,
                    cookie_name='jupyterhub-services',
                    base_url=service.prefix,
                )
                self.db.add(server)

            else:
                service.orm.server = None

            if service.oauth_available:
                self.oauth_provider.add_client(
                    client_id=service.oauth_client_id,
                    client_secret=service.api_token,
                    redirect_uri=service.oauth_redirect_uri,
                    description="JupyterHub service %s" % service.name,
                )

            self._service_map[name] = service

        # delete services from db not in service config:
        for service in self.db.query(orm.Service):
            if service.name not in self._service_map:
                self.db.delete(service)
        self.db.commit()

    async def start(self):
        """Start the whole thing.
           Overridden to make service.start() asynchronous."""

        self.io_loop = loop = IOLoop.current()

        if self.subapp:
            self.subapp.start()
            loop.stop()
            return

        if self.generate_config:
            self.write_config_file()
            loop.stop()
            return

        if self.generate_certs:
            self.load_config_file(self.config_file)
            if not self.internal_ssl:
                self.log.warning(
                    "You'll need to enable `internal_ssl` "
                    "in the `jupyterhub_config` file to use "
                    "these certs."
                )
                self.internal_ssl = True
            self.init_internal_ssl()
            self.log.info(
                "Certificates written to directory `{}`".format(
                    self.internal_certs_location
                )
            )
            loop.stop()
            return

        # start the proxy
        if self.proxy.should_start:
            try:
                await self.proxy.start()
            except Exception:
                self.log.critical("Failed to start proxy", exc_info=True)
                self.exit(1)
        else:
            self.log.info("Not starting proxy")

        # verify that we can talk to the proxy before listening.
        # avoids delayed failure if we can't talk to the proxy
        await self.proxy.get_all_routes()

        ssl_context = make_ssl_context(
            self.internal_ssl_key,
            self.internal_ssl_cert,
            cafile=self.internal_ssl_ca,
            check_hostname=False,
        )

        # start the webserver
        self.http_server = tornado.httpserver.HTTPServer(
            self.tornado_application,
            ssl_options=ssl_context,
            xheaders=True,
            trusted_downstream=self.trusted_downstream_ips,
        )
        bind_url = urlparse(self.hub.bind_url)
        try:
            if bind_url.scheme.startswith('unix+'):
                from tornado.netutil import bind_unix_socket

                socket = bind_unix_socket(unquote(bind_url.netloc))
                self.http_server.add_socket(socket)
            else:
                ip = bind_url.hostname
                port = bind_url.port
                if not port:
                    if bind_url.scheme == 'https':
                        port = 443
                    else:
                        port = 80
                self.http_server.listen(port, address=ip)
            self.log.info("Hub API listening on %s", self.hub.bind_url)
            if self.hub.url != self.hub.bind_url:
                self.log.info("Private Hub API connect url %s", self.hub.url)
        except Exception:
            self.log.error("Failed to bind hub to %s", self.hub.bind_url)
            raise

        # start the service(s)
        for service_name, service in self._service_map.items():
            msg = (
                '%s at %s' % (service_name, service.url)
                if service.url
                else service_name
            )
            if service.managed:
                self.log.info("Starting managed service %s", msg)
                try:
                    await service.start()
                except Exception:
                    self.log.critical(
                        "Failed to start service %s", service_name, exc_info=True
                    )
                    self.exit(1)
            else:
                self.log.info("Adding external service %s", msg)

            if service.url:
                tries = 10 if service.managed else 1
                for i in range(tries):
                    try:
                        ssl_context = make_ssl_context(
                            self.internal_ssl_key,
                            self.internal_ssl_cert,
                            cafile=self.internal_ssl_ca,
                        )
                        await Server.from_orm(service.orm.server).wait_up(
                            http=True, timeout=1, ssl_context=ssl_context
                        )
                    except TimeoutError:
                        if service.managed:
                            status = await service.spawner.poll()
                            if status is not None:
                                self.log.error(
                                    "Service %s exited with status %s",
                                    service_name,
                                    status,
                                )
                                break
                    else:
                        break
                else:
                    self.log.error(
                        "Cannot connect to %s service %s at %s. Is it running?",
                        service.kind,
                        service_name,
                        service.url,
                    )

        await self.proxy.check_routes(self.users, self._service_map)

        if self.service_check_interval and any(
            s.url for s in self._service_map.values()
        ):
            pc = PeriodicCallback(
                self.check_services_health, 1e3 * self.service_check_interval
            )
            pc.start()

        if self.last_activity_interval:
            pc = PeriodicCallback(
                self.update_last_activity, 1e3 * self.last_activity_interval
            )
            self.last_activity_callback = pc
            pc.start()

        self.log.info("JupyterHub is now running at %s", self.proxy.public_url)
        # Use atexit for Windows, it doesn't have signal handling support
        if os.name == "nt":
            atexit.register(self.atexit)
        # register cleanup on both TERM and INT
        self.init_signal()
        self._start_future.set_result(None)

main = JupyterHub.launch_instance
