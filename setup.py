from setuptools import setup

if __name__ == "__main__":
    setup(name="kubespawner_service_jupyterhub",
          version="0.1",
          description="JupyterHub with suppot for spawning JupyterHub services via kubespawner",
          url="https://github.com/rkevin-arch/kubespawner_service_jupyterhub",
          author="rkevin",
          license="BSD",
          packages=["kubespawner_service_jupyterhub"],
          entry_points={"console_scripts":["jupyterhub = kubespawner_service_jupyterhub.app:main"]}
    )
