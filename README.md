# JupyterHub with kubespawner-managed services

Hacked-together module that overrides some functions in JupyterHub proper, to enable spawning JupyterHub managed services via `kubespawner` and attach persistent storage to it.

I'll test this a bit further, ask for feedback from the JupyterHub community, try to write down a list of configurable options for kube-managed services, and then submit a proper pull request.

## Usage

You may add two extra keys to an entry in `c.JupyterHub.services`: `image` and `port`. If you do not set `image`, then the default behavior applies (subprocess managed service or external service), otherwise it's a service that should be spawned in Kubernetes. JupyterHub will create a pod, give it some storage using PersistentVolumes mounted at `/srv/{SERVICE_NAME}`, and proxy requests to it like a subprocess managed service.

## Modifications

I've overridden two functions in the main application in `app.py`. One of them is `init_services` to parse more info from `c.JupyterHub.services` and pass them into the constructor of the modified `Service` class. The other is `start`, just to make it asynchronous since `kubespawner.start()` wants to be awaited.

In `service.py`, there is now a new `_KubeServiceSpawner` class based on regular kubespawner, but with modifications to environment variables so it suits services better rather than notebook servers. The `Service` class now parses the options `image` and `port`, and if it's a kube managed service then uses `_KubeServiceSpawner` to spawn the pod and set variables passed to the proxy correctly. Otherwise, the default behavior of `jupyterhub.services.service` applies.

## Testing

This is developed for [ngshare](https://github.com/lxylxy123456/ngshare), so for the testing setup I'm using please consult [here](https://github.com/lxylxy123456/ngshare/blob/master/testing/README.md).

I have modified the helm chart in the testing setup, but I believe this is no longer necessary for this setup. I'll test this later.

I'll put a demo testing setup running the sample `whoami` service here later.
