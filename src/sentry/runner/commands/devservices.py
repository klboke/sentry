from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, Generator, Literal, overload

import click
import requests

if TYPE_CHECKING:
    import docker

# Work around a stupid docker issue: https://github.com/docker/for-mac/issues/5025
RAW_SOCKET_HACK_PATH = os.path.expanduser(
    "~/Library/Containers/com.docker.docker/Data/docker.raw.sock"
)
if os.path.exists(RAW_SOCKET_HACK_PATH):
    os.environ.setdefault("DOCKER_HOST", "unix://" + RAW_SOCKET_HACK_PATH)

# assigned as a constant so mypy's "unreachable" detection doesn't fail on linux
# https://github.com/python/mypy/issues/12286
DARWIN = sys.platform == "darwin"


@contextlib.contextmanager
def get_docker_client() -> Generator[docker.DockerClient, None, None]:
    import docker

    with contextlib.closing(docker.from_env()) as client:
        try:
            client.ping()
        except (requests.exceptions.ConnectionError, docker.errors.APIError):
            click.echo("Attempting to start docker...")
            if DARWIN:
                subprocess.check_call(
                    ("open", "-a", "/Applications/Docker.app", "--args", "--unattended")
                )
            else:
                click.echo("Unable to start docker.")
                raise click.ClickException("Make sure docker is running.")

            max_wait = 60
            timeout = time.monotonic() + max_wait

            click.echo(f"Waiting for docker to be ready.... (timeout in {max_wait}s)")
            while time.monotonic() < timeout:
                time.sleep(1)
                try:
                    client.ping()
                except (requests.exceptions.ConnectionError, docker.errors.APIError):
                    continue
                else:
                    break
            else:
                raise click.ClickException("Failed to start docker.")

        yield client


@overload
def get_or_create(
    client: docker.DockerClient, thing: Literal["network"], name: str
) -> docker.models.networks.Network:
    ...


@overload
def get_or_create(
    client: docker.DockerClient, thing: Literal["volume"], name: str
) -> docker.models.volumes.Volume:
    ...


def get_or_create(
    client: docker.DockerClient, thing: Literal["network", "volume"], name: str
) -> docker.models.networks.Network | docker.models.volumes.Volume:
    from docker.errors import NotFound

    try:
        return getattr(client, thing + "s").get(name)
    except NotFound:
        click.secho(f"> Creating '{name}' {thing}", err=True, fg="yellow")
        return getattr(client, thing + "s").create(name)


def retryable_pull(client: docker.DockerClient, image: str, max_attempts: int = 5) -> None:
    from docker.errors import APIError

    current_attempt = 0

    # `client.images.pull` intermittently fails in CI, and the docker API/docker-py does not give us the relevant error message (i.e. it's not the same error as running `docker pull` from shell)
    # As a workaround, let's retry when we hit the ImageNotFound exception.
    #
    # See https://github.com/docker/docker-py/issues/2101 for more information
    while True:
        try:
            client.images.pull(image)
        except APIError:
            if current_attempt + 1 >= max_attempts:
                raise
            current_attempt = current_attempt + 1
            continue
        else:
            break


def ensure_interface(ports: dict[str, int | tuple[str, int]]) -> dict[str, tuple[str, int]]:
    # If there is no interface specified, make sure the
    # default interface is 127.0.0.1
    rv = {}
    for k, v in ports.items():
        if not isinstance(v, tuple):
            v = ("127.0.0.1", v)
        rv[k] = v
    return rv


@click.group()
def devservices() -> None:
    """
    Manage dependent development services required for Sentry.

    Do not use in production!
    """
    # Disable backend validation so no devservices commands depend on like,
    # redis to be already running.
    os.environ["SENTRY_SKIP_BACKEND_VALIDATION"] = "1"


@devservices.command()
@click.option("--project", default="sentry")
@click.option("--fast", is_flag=True, default=False, help="Never pull and reuse containers.")
@click.argument("service", nargs=1)
def attach(project: str, fast: bool, service: str) -> None:
    """
    Run a single devservice in the foreground.

    Accepts a single argument, the name of the service to spawn. The service
    will run with output printed to your terminal, and the ability to kill it
    with ^C. This is used in devserver.

    Note: This does not update images, you will have to use `devservices up`
    for that.
    """
    from sentry.runner import configure

    configure()

    containers = _prepare_containers(project, silent=True)
    if service not in containers:
        raise click.ClickException(f"Service `{service}` is not known or not enabled.")

    with get_docker_client() as docker_client:
        container = _start_service(
            docker_client,
            service,
            containers,
            project,
            fast=fast,
            always_start=True,
        )

        def exit_handler(*_: Any) -> None:
            try:
                click.echo(f"Stopping {service}")
                container.stop()
                click.echo(f"Removing {service}")
                container.remove()
            except KeyboardInterrupt:
                pass

        signal.signal(signal.SIGINT, exit_handler)
        signal.signal(signal.SIGTERM, exit_handler)

        for line in container.logs(stream=True, since=int(time.time() - 20)):
            click.echo(line, nl=False)


@devservices.command()
@click.argument("services", nargs=-1)
@click.option("--project", default="sentry")
@click.option("--exclude", multiple=True, help="Service to ignore and not run. Repeatable option.")
@click.option("--fast", is_flag=True, default=False, help="Never pull and reuse containers.")
@click.option(
    "--skip-only-if", is_flag=True, default=False, help="Skip 'only_if' checks for services"
)
def up(
    services: list[str],
    project: str,
    exclude: list[str],
    fast: bool,
    skip_only_if: bool,
) -> None:
    """
    Run/update all devservices in the background.

    The default is everything, however you may pass positional arguments to specify
    an explicit list of services to bring up.

    You may also exclude services, for example: --exclude redis --exclude postgres.
    """
    from sentry.runner import configure

    configure()

    containers = _prepare_containers(
        project, skip_only_if=(skip_only_if or len(services) > 0), silent=True
    )
    selected_services = set()

    if services:
        for service in services:
            if service not in containers:
                click.secho(
                    f"Service `{service}` is not known or not enabled.\n",
                    err=True,
                    fg="red",
                )
                click.secho(
                    "Services that are available:\n" + "\n".join(containers.keys()) + "\n", err=True
                )
                raise click.Abort()
            selected_services.add(service)
    else:
        selected_services = set(containers.keys())

    for service in exclude:
        if service not in containers:
            click.secho(f"Service `{service}` is not known or not enabled.\n", err=True, fg="red")
            click.secho(
                "Services that are available:\n" + "\n".join(containers.keys()) + "\n", err=True
            )
            raise click.Abort()
        selected_services.remove(service)

    if fast:
        click.secho(
            "> Warning! Fast mode completely eschews any image updating, so services may be stale.",
            err=True,
            fg="red",
        )

    with get_docker_client() as docker_client:
        get_or_create(docker_client, "network", project)

        with ThreadPoolExecutor(max_workers=len(selected_services)) as executor:
            futures = []
            for name in selected_services:
                futures.append(
                    executor.submit(
                        _start_service,
                        docker_client,
                        name,
                        containers,
                        project,
                        fast=fast,
                    )
                )
            for future in as_completed(futures):
                # If there was an exception, reraising it here to the main thread
                # will not terminate the whole python process. We'd like to report
                # on this exception and stop as fast as possible, so terminate
                # ourselves. I believe (without verification) that the OS is now
                # free to cleanup these threads, but not sure if they'll remain running
                # in the background. What matters most is that we regain control
                # of the terminal.
                e = future.exception()
                if e:
                    click.echo(e)
                    me = os.getpid()
                    os.kill(me, signal.SIGTERM)


def _prepare_containers(
    project: str, skip_only_if: bool = False, silent: bool = False
) -> dict[str, Any]:
    from django.conf import settings

    from sentry import options as sentry_options

    containers = {}

    for name, option_builder in settings.SENTRY_DEVSERVICES.items():
        options = option_builder(settings, sentry_options)
        only_if = options.pop("only_if", True)

        if not skip_only_if and not only_if:
            if not silent:
                click.secho(f"! Skipping {name} due to only_if condition", err=True, fg="cyan")
            continue

        options["network"] = project
        options["detach"] = True
        options["name"] = project + "_" + name
        options.setdefault("ports", {})
        options.setdefault("environment", {})
        # set policy to unless-stopped to avoid automatically restarting containers on boot
        # this is important given you can start multiple sets of containers that can conflict
        # with each other
        options.setdefault("restart_policy", {"Name": "unless-stopped"})
        options["ports"] = ensure_interface(options["ports"])
        containers[name] = options

    # keys are service names
    # a service has 1 container exactly, the container name being value["name"]
    return containers


@overload
def _start_service(
    client: docker.DockerClient,
    name: str,
    containers: dict[str, Any],
    project: str,
    fast: bool = False,
    always_start: Literal[True] = ...,
) -> docker.models.containers.Container:
    ...


@overload
def _start_service(
    client: docker.DockerClient,
    name: str,
    containers: dict[str, Any],
    project: str,
    fast: bool = False,
    always_start: bool = False,
) -> docker.models.containers.Container | None:
    ...


def _start_service(
    client: docker.DockerClient,
    name: str,
    containers: dict[str, Any],
    project: str,
    fast: bool = False,
    always_start: bool = False,
) -> docker.models.containers.Container | None:
    from docker.errors import NotFound

    options = containers[name]

    for key, value in list(options["environment"].items()):
        options["environment"][key] = value.format(containers=containers)

    pull = options.pop("pull", False)
    if not fast:
        if pull:
            click.secho(f"> Pulling image '{options['image']}'", fg="green")
            retryable_pull(client, options["image"])
        else:
            # We want make sure to pull everything on the first time,
            # (the image doesn't exist), regardless of pull=True.
            try:
                client.images.get(options["image"])
            except NotFound:
                click.secho(f"> Pulling image '{options['image']}'", fg="green")
                retryable_pull(client, options["image"])

    for mount in list(options.get("volumes", {}).keys()):
        if "/" not in mount:
            get_or_create(client, "volume", project + "_" + mount)
            options["volumes"][project + "_" + mount] = options["volumes"].pop(mount)

    listening = ""
    if options["ports"]:
        listening = "(listening: %s)" % ", ".join(map(str, options["ports"].values()))

    # If a service is associated with the devserver, then do not run the created container.
    # This was mainly added since it was not desirable for nginx to occupy port 8000 on the
    # first "devservices up".
    # Nowadays that nginx is gone again, it's still nice to be able to shut
    # down services within devserver.
    # See https://github.com/getsentry/sentry/pull/18362#issuecomment-616785458
    with_devserver = options.pop("with_devserver", False)

    # Two things call _start_service.
    # devservices up, and devservices attach.
    # Containers that should be started on-demand with devserver
    # should ONLY be started via the latter, which sets `always_start`.
    if with_devserver and not always_start:
        click.secho(
            f"> Not starting container '{options['name']}' because it should be started on-demand with devserver.",
            fg="yellow",
        )
        # XXX: if always_start=False, do not expect to have a container returned 100% of the time.
        return None

    container = None
    try:
        container = client.containers.get(options["name"])
    except NotFound:
        pass

    if container is not None:
        # devservices which are marked with pull True will need their containers
        # to be recreated with the freshly pulled image.
        should_reuse_container = not pull

        # Except if the container is started as part of devserver we should reuse it.
        # Or, if we're in fast mode (devservices up --fast)
        if with_devserver or fast:
            should_reuse_container = True

        if should_reuse_container:
            click.secho(
                f"> Starting EXISTING container '{container.name}' {listening}",
                fg="yellow",
            )
            # Note that if the container is already running, this will noop.
            # This makes repeated `devservices up` quite fast.
            container.start()
            return container

        click.secho(f"> Stopping container '{container.name}'", fg="yellow")
        container.stop()
        click.secho(f"> Removing container '{container.name}'", fg="yellow")
        container.remove()

    click.secho(f"> Creating container '{options['name']}'", fg="yellow")
    container = client.containers.create(**options)
    click.secho(f"> Starting container '{container.name}' {listening}", fg="yellow")
    container.start()
    return container


@devservices.command()
@click.option("--project", default="sentry")
@click.argument("service", nargs=-1)
def down(project: str, service: list[str]) -> None:
    """
    Shut down services without deleting their underlying containers and data.
    Useful if you want to temporarily relieve resources on your computer.

    The default is everything, however you may pass positional arguments to specify
    an explicit list of services to bring down.
    """
    # TODO: make more like devservices rm

    def _down(container: docker.models.containers.Container) -> None:
        click.secho(f"> Stopping '{container.name}' container", fg="red")
        container.stop()
        click.secho(f"> Stopped '{container.name}' container", fg="red")

    containers = []
    prefix = f"{project}_"

    with get_docker_client() as docker_client:
        for container in docker_client.containers.list(all=True):
            if not container.name.startswith(prefix):
                continue
            if service and not container.name[len(prefix) :] in service:
                continue
            containers.append(container)

        with ThreadPoolExecutor(max_workers=len(containers)) as executor:
            futures = []
            for container in containers:
                futures.append(executor.submit(_down, container))
            for future in as_completed(futures):
                # If there was an exception, reraising it here to the main thread
                # will not terminate the whole python process. We'd like to report
                # on this exception and stop as fast as possible, so terminate
                # ourselves. I believe (without verification) that the OS is now
                # free to cleanup these threads, but not sure if they'll remain running
                # in the background. What matters most is that we regain control
                # of the terminal.
                e = future.exception()
                if e:
                    click.echo(e)
                    me = os.getpid()
                    os.kill(me, signal.SIGTERM)


@devservices.command()
@click.option("--project", default="sentry")
@click.argument("services", nargs=-1)
def rm(project: str, services: list[str]) -> None:
    """
    Shut down and delete all services and associated data.
    Useful if you'd like to start with a fresh slate.

    The default is everything, however you may pass positional arguments to specify
    an explicit list of services to remove.
    """
    from docker.errors import NotFound

    from sentry.runner import configure

    configure()

    containers = _prepare_containers(project, silent=True)

    if services:
        selected_containers = {}
        for service in services:
            # XXX: This code is also fairly duplicated in here at this point, so dedupe in the future.
            if service not in containers:
                click.secho(
                    f"Service `{service}` is not known or not enabled.\n",
                    err=True,
                    fg="red",
                )
                click.secho(
                    "Services that are available:\n" + "\n".join(containers.keys()) + "\n", err=True
                )
                raise click.Abort()
            selected_containers[service] = containers[service]
        containers = selected_containers

    click.confirm(
        """
This will delete these services and all of their data:

%s

Are you sure you want to continue?"""
        % "\n".join(containers.keys()),
        abort=True,
    )

    with get_docker_client() as docker_client:
        volume_to_service = {}
        for service_name, container_options in containers.items():
            try:
                container = docker_client.containers.get(container_options["name"])
            except NotFound:
                click.secho(
                    "> WARNING: non-existent container '%s'" % container_options["name"],
                    err=True,
                    fg="yellow",
                )
                continue

            click.secho("> Stopping '%s' container" % container_options["name"], err=True, fg="red")
            container.stop()
            click.secho("> Removing '%s' container" % container_options["name"], err=True, fg="red")
            container.remove()
            for volume in container_options.get("volumes") or ():
                volume_to_service[volume] = service_name

        prefix = project + "_"

        for volume in docker_client.volumes.list():
            if volume.name.startswith(prefix):
                local_name = volume.name[len(prefix) :]
                if not services or volume_to_service.get(local_name) in services:
                    click.secho("> Removing '%s' volume" % volume.name, err=True, fg="red")
                    volume.remove()

        if not services:
            try:
                network = docker_client.networks.get(project)
            except NotFound:
                pass
            else:
                click.secho("> Removing '%s' network" % network.name, err=True, fg="red")
                network.remove()
