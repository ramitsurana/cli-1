from __future__ import print_function
import getpass
import json
import sys
import os
import logging
from os.path import join, expanduser, abspath
import ConfigParser
import select
import termios
import tty
import signal
import errno
import urllib

import websocket
import tutum
import docker
import yaml
from tutum.api import auth
from tutum.api import exceptions
from tutum import TutumAuthError, TutumApiError, ObjectNotFound, NonUniqueIdentifier

from exceptions import StreamOutputError
from tutumcli import utils


TUTUM_FILE = '.tutum'
AUTH_SECTION = 'auth'
USER_OPTION = "user"
APIKEY_OPTION = 'apikey'
AUTH_ERROR = 'auth_error'
NO_ERROR = 'no_error'

TUTUM_AUTH_ERROR_EXIT_CODE = 2
EXCEPTION_EXIT_CODE = 3

cli_log = logging.getLogger("cli")


def login(username, password, email):
    if not username and not password:
        username = raw_input('Username: ')
        password = getpass.getpass()
    elif not username:
        username = raw_input('Username: ')
    elif not password:
        password = getpass.getpass()
    try:
        user, api_key = auth.get_auth(username, password)
        if api_key is not None:
            config = ConfigParser.ConfigParser()
            config.add_section(AUTH_SECTION)
            config.set(AUTH_SECTION, USER_OPTION, user)
            config.set(AUTH_SECTION, APIKEY_OPTION, api_key)
            with open(join(expanduser('~'), TUTUM_FILE), 'w') as cfgfile:
                config.write(cfgfile)
            print("Login succeeded!")
    except exceptions.TutumAuthError:
        registered, text = utils.try_register(username, password, email)
        if registered:
            print(text)
        else:
            if 'username: A user with that username already exists.' in text:
                print("Wrong username and/or password. Please try to login again", file=sys.stderr)
                sys.exit(TUTUM_AUTH_ERROR_EXIT_CODE)
            else:
                text = text.replace('password1', 'password')
                text = text.replace('password2', 'password')
                text = text.replace('\npassword: This field is required.', '', 1)
                print(text, file=sys.stderr)
                sys.exit(TUTUM_AUTH_ERROR_EXIT_CODE)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def verify_auth(args):
    def _login():
        username = raw_input("Username: ")
        password = getpass.getpass()
        try:
            user, api_key = auth.get_auth(username, password)
            if api_key is not None:
                config = ConfigParser.ConfigParser()
                config.add_section(AUTH_SECTION)
                config.set(AUTH_SECTION, USER_OPTION, user)
                config.set(AUTH_SECTION, APIKEY_OPTION, api_key)
                with open(join(expanduser('~'), TUTUM_FILE), 'w') as cfgfile:
                    config.write(cfgfile)
                return True
        except tutum.TutumAuthError:
            return False
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(EXCEPTION_EXIT_CODE)

    if args.cmd != 'login':
        try:
            tutum.api.http.send_request("GET", "/auth")
        except tutum.TutumAuthError:
            print("Not Authorized, Please login:", file=sys.stderr)
            while True:
                success = _login()
                if success:
                    print("Login succeeded!")
                    # Update user and apikey for SDK
                    tutum.user = auth.load_from_file()[0] or os.environ.get('TUTUM_USER', None)
                    tutum.apikey = auth.load_from_file()[1] or os.environ.get('TUTUM_APIKEY', None)
                    break
                else:
                    print("Not Authorized, Please login:", file=sys.stderr)


def build(tag, working_directory, docker_sock):
    build_image = "tutum/builder:latest"
    if not docker_sock:
        docker_sock = "/var/run/docker.sock"
    try:
        docker_client = utils.get_docker_client()
        binds = {
            abspath(working_directory):
                {
                    'bind': "/app",
                    'ro': False
                }
        }
        binds[docker_sock] = \
            {
                'bind': "/var/run/docker.sock",
                'ro': False
            }

        output = docker_client.pull(build_image, stream=True)
        utils.stream_output(output, sys.stdout)
        container = docker_client.create_container(image=build_image, environment={"IMAGE_NAME": tag})
        docker_client.start(container=container.get("Id"), privileged=True, binds=binds)
        output = docker_client.attach(container.get("Id"), stream=True)
        for chunck in output:
            print(chunck, end="")
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def event():
    try:
        events = tutum.TutumEvents()
        events.on_message(lambda e: print(e))
        events.run_forever()
    except KeyboardInterrupt:
        pass


def service_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            print(json.dumps(service.get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_logs(identifiers, tail, follow):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            service.logs(tail, follow)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_ps(quiet, status, stack):
    try:
        headers = ["NAME", "UUID", "STATUS", "#CONTAINERS", "IMAGE", "DEPLOYED", "PUBLIC DNS", "STACK"]

        stack_resource_uri = None
        if stack:
            s = tutum.Utils.fetch_remote_stack(stack, raise_exceptions=False)
            if isinstance(s, NonUniqueIdentifier):
                raise NonUniqueIdentifier("Identifier %s matches more than one stack, please use UUID instead" % stack)
            if isinstance(s, ObjectNotFound):
                raise ObjectNotFound("Identifier '%s' does not match any stack" % stack)
            stack_resource_uri = s.resource_uri
        service_list = tutum.Service.list(state=status, stack=stack_resource_uri)

        data_list = []
        long_uuid_list = []
        has_unsynchronized_service = False
        stacks = {}
        for stack in tutum.Stack.list():
            stacks[stack.resource_uri] = stack.name
        for service in service_list:
            service_state = utils.add_unicode_symbol_to_state(service.state)
            if not service.synchronized and service.state != "Redeploying":
                service_state += "(*)"
                has_unsynchronized_service = True
            data_list.append([service.name, service.uuid[:8],
                              service_state,
                              service.current_num_containers,
                              service.image_name,
                              utils.get_humanize_local_datetime_from_utc_datetime_string(service.deployed_datetime),
                              service.public_dns,
                              stacks.get(service.stack)])
            long_uuid_list.append(service.uuid)
        if len(data_list) == 0:
            data_list.append(["", "", "", "", "", ""])

        if quiet:
            for uuid in long_uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
            if has_unsynchronized_service:
                print(
                    "\n(*) Please note that this service needs to be redeployed to have its configuration changes applied")
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def service_redeploy(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            result = service.redeploy()
            utils.sync_action(service, sync)
            if result:
                print(service.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_create(image, name, cpu_shares, memory, privileged, target_num_containers, run_command, entrypoint,
                   expose, publish, envvars, envfiles, tag, linked_to_service, autorestart, autodestroy, autoredeploy,
                   roles, sequential, volume, volumes_from, deployment_strategy, sync):
    try:
        ports = utils.parse_published_ports(publish)

        # Add exposed_port to ports, excluding whose inner_port that has been defined in published ports
        exposed_ports = utils.parse_exposed_ports(expose)
        for exposed_port in exposed_ports:
            existed = False
            for port in ports:
                if exposed_port.get('inner_port', '') == port.get('inner_port', ''):
                    existed = True
                    break
            if not existed:
                ports.append(exposed_port)

        envvars = utils.parse_envvars(envvars, envfiles)
        links_service = utils.parse_links(linked_to_service, 'to_service')

        tags = []
        if tag:
            if isinstance(tag, list):
                for t in tag:
                    tags.append({"name": t})
            else:
                tags.append({"name": tag})

        bindings = utils.parse_volume(volume)
        bindings.extend(utils.parse_volumes_from(volumes_from))

        service = tutum.Service.create(image=image, name=name, cpu_shares=cpu_shares,
                                       memory=memory, privileged=privileged,
                                       target_num_containers=target_num_containers, run_command=run_command,
                                       entrypoint=entrypoint, container_ports=ports, container_envvars=envvars,
                                       linked_to_service=links_service,
                                       autorestart=autorestart, autodestroy=autodestroy, autoredeploy=autoredeploy,
                                       roles=roles, sequential_deployment=sequential, tags=tags, bindings=bindings,
                                       deployment_strategy=deployment_strategy)
        result = service.save()
        utils.sync_action(service, sync)
        if result:
            print(service.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def service_run(image, name, cpu_shares, memory, privileged, target_num_containers, run_command, entrypoint,
                expose, publish, envvars, envfiles, tag, linked_to_service, autorestart, autodestroy, autoredeploy,
                roles, sequential, volume, volumes_from, deployment_strategy, sync):
    try:
        ports = utils.parse_published_ports(publish)

        # Add exposed_port to ports, excluding whose inner_port that has been defined in published ports
        exposed_ports = utils.parse_exposed_ports(expose)
        for exposed_port in exposed_ports:
            existed = False
            for port in ports:
                if exposed_port.get('inner_port', '') == port.get('inner_port', ''):
                    existed = True
                    break
            if not existed:
                ports.append(exposed_port)

        envvars = utils.parse_envvars(envvars, envfiles)
        links_service = utils.parse_links(linked_to_service, 'to_service')

        tags = []
        if tag:
            if isinstance(tag, list):
                for t in tag:
                    tags.append({"name": t})
            else:
                tags.append({"name": tag})

        bindings = utils.parse_volume(volume)
        bindings.extend(utils.parse_volumes_from(volumes_from))

        service = tutum.Service.create(image=image, name=name, cpu_shares=cpu_shares,
                                       memory=memory, privileged=privileged,
                                       target_num_containers=target_num_containers, run_command=run_command,
                                       entrypoint=entrypoint, container_ports=ports, container_envvars=envvars,
                                       linked_to_service=links_service,
                                       autorestart=autorestart, autodestroy=autodestroy, autoredeploy=autoredeploy,
                                       roles=roles, sequential_deployment=sequential, tags=tags, bindings=bindings,
                                       deployment_strategy=deployment_strategy)
        service.save()
        result = service.start()
        utils.sync_action(service, sync)
        if result:
            print(service.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def service_scale(identifiers, target_num_containers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            service.target_num_containers = target_num_containers
            service.save()
            result = service.scale()
            utils.sync_action(service, sync)
            if result:
                print(service.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_set(identifiers, image, cpu_shares, memory, privileged, target_num_containers, run_command, entrypoint,
                expose, publish, envvars, envfiles, tag, linked_to_service, autorestart, autodestroy, autoredeploy,
                roles, sequential, redeploy, volume, volumes_from, deployment_strategy, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier, raise_exceptions=True)
            if service is not None:
                if image:
                    service.image = image
                if cpu_shares:
                    service.cpu_shares = cpu_shares
                if memory:
                    service.memory = memory
                if privileged:
                    service.privileged = privileged
                if target_num_containers:
                    service.target_num_containers = target_num_containers
                if run_command:
                    service.run_command = run_command
                if entrypoint:
                    service.entrypoint = entrypoint

                ports = utils.parse_published_ports(publish)
                # Add exposed_port to ports, excluding whose inner_port that has been defined in published ports
                exposed_ports = utils.parse_exposed_ports(expose)
                for exposed_port in exposed_ports:
                    existed = False
                    for port in ports:
                        if exposed_port.get('inner_port', '') == port.get('inner_port', ''):
                            existed = True
                            break
                    if not existed:
                        ports.append(exposed_port)
                if ports:
                    service.container_ports = ports

                envvars = utils.parse_envvars(envvars, envfiles)
                if envvars:
                    service.container_envvars = envvars

                if tag:
                    service.tags = []
                    for t in tag:
                        new_tag = {"name": t}
                        if new_tag not in service.tags:
                            service.tags.append(new_tag)
                    service.__addchanges__("tags")

                links_service = utils.parse_links(linked_to_service, 'to_service')
                if linked_to_service:
                    service.linked_to_service = links_service

                if autorestart:
                    service.autorestart = autorestart

                if autodestroy:
                    service.autodestroy = autodestroy

                if autoredeploy:
                    service.autoredeploy = autoredeploy

                if roles:
                    service.roles = roles

                if sequential:
                    service.sequential_deployment = sequential

                bindings = utils.parse_volume(volume)
                bindings.extend(utils.parse_volumes_from(volumes_from))
                if bindings:
                    service.bindings = bindings

                if deployment_strategy:
                    service.deployment_strategy = deployment_strategy

                result = service.save()
                utils.sync_action(service, sync)
                if result:
                    if redeploy:
                        print("Redeploying Service ...")
                        result2 = service.redeploy()
                        if result2:
                            print(service.uuid)
                    else:
                        print(service.uuid)
                        print("Service must be redeployed to have its configuration changes applied.")
                        print("To redeploy execute: $ tutum service redeploy", identifier)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_start(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            result = service.start()
            utils.sync_action(service, sync)
            if result:
                print(service.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_stop(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            result = service.stop()
            utils.sync_action(service, sync)
            if result:
                print(service.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def service_terminate(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            service = tutum.Utils.fetch_remote_service(identifier)
            result = service.delete()
            utils.sync_action(service, sync)
            if result:
                print(service.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def container_exec(identifier, command):
    def invoke_shell(url):
        shell = websocket.create_connection(url, timeout=10)

        oldtty = termios.tcgetattr(sys.stdin)
        old_handler = signal.getsignal(signal.SIGWINCH)
        errorcode = 0

        try:
            tty.setraw(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())

            while True:
                try:
                    r, w, e = select.select([shell.sock, sys.stdin], [], [shell.sock], 5)
                    if sys.stdin in r:
                        x = sys.stdin.read(1)
                        # read arrows
                        if x == '\x1b':
                            x += sys.stdin.read(1)
                            if x[1] == '[':
                                x += sys.stdin.read(1)
                        if len(x) == 0:
                            shell.send('\n')
                        shell.send(x)

                    if shell.sock in r:
                        data = shell.recv()
                        if not data:
                            continue
                        try:
                            message = json.loads(data)
                            if message.get("type") == "error":
                                if message.get("data", {}).get("errorMessage") == "UNAUTHORIZED":
                                    raise TutumAuthError
                                else:
                                    raise TutumApiError(message)
                            streamType = message.get("streamType")
                            if streamType == "stdout":
                                sys.stdout.write(message.get("output"))
                                sys.stdout.flush()
                            elif streamType == "stderr":
                                sys.stderr.write(message.get("output"))
                                sys.stderr.flush()
                        except TutumAuthError:
                            raise
                        except:
                            sys.stdout.write(data)
                            sys.stdout.flush()
                except (select.error, IOError) as e:
                    if e.args and e.args[0] == errno.EINTR:
                        pass
                    else:
                        raise
        except TutumAuthError:
            sys.stderr.write("Not Authorized\r\n")
            sys.stderr.flush()
            errorcode = TUTUM_AUTH_ERROR_EXIT_CODE
        except websocket.WebSocketConnectionClosedException:
            pass
        except websocket.WebSocketException:
            sys.stderr.write("Connection is already closed.\r\n")
            sys.stderr.flush()
            errorcode = EXCEPTION_EXIT_CODE
        except Exception as e:
            sys.stderr.write("%s\r\n" % e)
            sys.stderr.flush()
            errorcode = EXCEPTION_EXIT_CODE
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, oldtty)
            signal.signal(signal.SIGWINCH, old_handler)
            exit(errorcode)

    try:
        container = tutum.Utils.fetch_remote_container(identifier)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)

    if tutum.tutum_auth:
        endpoint = "container/%s/exec/?auth=%s" % (container.uuid, urllib.quote_plus(tutum.tutum_auth))
    else:
        endpoint = "container/%s/exec/?user=%s&token=%s" % (container.uuid, tutum.user, tutum.apikey)

    if command:
        escaped_cmd = []
        for c in command:
            if r'"' in c:
                c = c.replace(r'"', r'\"')
            if " " in c:
                c = '"%s"' % c
            escaped_cmd.append(c)

        escaped_cmd = " ".join(escaped_cmd)
        cli_log.debug("escaped command: %s" % escaped_cmd)
        endpoint = "%s&command=%s" % (endpoint, urllib.quote_plus(escaped_cmd))

    url = "/".join([tutum.stream_url.rstrip("/"), endpoint.lstrip('/')])
    cli_log.debug("websocket url: %s" % url)
    invoke_shell(url)


def container_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            container = tutum.Utils.fetch_remote_container(identifier)
            print(json.dumps(container.get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def container_logs(identifiers, tail, follow):
    has_exception = False
    for identifier in identifiers:
        try:
            container = tutum.Utils.fetch_remote_container(identifier)
            container.logs(tail, follow)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def container_redeploy(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            container = tutum.Utils.fetch_remote_container(identifier)
            result = container.redeploy()
            utils.sync_action(container, sync)
            if result:
                print(container.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def container_ps(quiet, status, service, no_trunc):
    try:
        headers = ["NAME", "UUID", "STATUS", "IMAGE", "RUN COMMAND", "EXIT CODE", "DEPLOYED", "PORTS", "NODE", "STACK"]

        service_resrouce_uri = None
        if service:
            s = tutum.Utils.fetch_remote_service(service, raise_exceptions=False)
            if isinstance(s, NonUniqueIdentifier):
                raise NonUniqueIdentifier(
                    "Identifier %s matches more than one service, please use UUID instead" % service)
            if isinstance(s, ObjectNotFound):
                raise ObjectNotFound("Identifier '%s' does not match any service" % service)
            service_resrouce_uri = s.resource_uri

        containers = tutum.Container.list(state=status, service=service_resrouce_uri)

        data_list = []
        long_uuid_list = []
        stacks = {}
        for stack in tutum.Stack.list():
            stacks[stack.resource_uri] = stack.name
        services = {}
        for s in tutum.Service.list():
            services[s.resource_uri] = s.stack
        nodes = {}
        for n in tutum.Node.list():
            nodes[n.resource_uri] = n.uuid

        for container in containers:
            ports = []
            for index, port in enumerate(container.container_ports):
                ports_string = ""
                if port['outer_port'] is not None:
                    ports_string += "%s:%d->" % (container.public_dns, port['outer_port'])
                ports_string += "%d/%s" % (port['inner_port'], port['protocol'])
                ports.append(ports_string)

            container_uuid = container.uuid
            run_command = container.run_command
            ports_string = ", ".join(ports)
            node = nodes.get(container.node)
            if not no_trunc:
                container_uuid = container_uuid[:8]

                if run_command and len(run_command) > 20:
                    run_command = run_command[:17] + '...'
                if ports_string and len(ports_string) > 20:
                    ports_string = ports_string[:17] + '...'
                node = node[:8]

            data_list.append([container.name,
                              container_uuid,
                              utils.add_unicode_symbol_to_state(container.state),
                              container.image_name,
                              run_command,
                              container.exit_code,
                              utils.get_humanize_local_datetime_from_utc_datetime_string(container.deployed_datetime),
                              ports_string,
                              node,
                              stacks.get(services.get(container.service))])
            long_uuid_list.append(container.uuid)
        if len(data_list) == 0:
            data_list.append(["", "", "", "", "", "", "", "", ""])
        if quiet:
            for uuid in long_uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def container_start(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            container = tutum.Utils.fetch_remote_container(identifier)
            result = container.start()
            utils.sync_action(container, sync)
            if result:
                print(container.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def container_stop(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            container = tutum.Utils.fetch_remote_container(identifier)
            result = container.stop()
            utils.sync_action(container, sync)
            if result:
                print(container.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def container_terminate(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            container = tutum.Utils.fetch_remote_container(identifier)
            result = container.delete()
            utils.sync_action(container, sync)
            if result:
                print(container.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def image_list(quiet, jumpstarts, linux):
    try:
        headers = ["NAME", "DESCRIPTION"]
        data_list = []
        name_list = []
        if jumpstarts:
            image_list = tutum.Image.list(starred=True)
        elif linux:
            image_list = tutum.Image.list(base_image=True)
        else:
            image_list = tutum.Image.list(is_private_image=True)
        if len(image_list) != 0:
            for image in image_list:
                data_list.append([image.name, image.description])
                name_list.append(image.name)
        else:
            data_list.append(["", ""])

        if quiet:
            for name in name_list:
                print(name)
        else:
            utils.tabulate_result(data_list, headers)

    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def image_register(repository, description, username, password, sync):
    if not username and not password:
        print('Please input username and password of the registry:')
        username = raw_input('Username: ')
        password = getpass.getpass()
    elif not username:
        print('Please input username of the registry:')
        username = raw_input('Username: ')
    elif not password:
        print('Please input password of the registry:')
        password = getpass.getpass()
    try:
        image = tutum.Image.create(name=repository, username=username, password=password, description=description)
        result = image.save()
        utils.sync_action(image, sync)
        if result:
            print(image.name)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def image_push(name, public):
    def push_to_public(repository):
        print('Pushing %s to public registry ...' % repository)

        output_status = NO_ERROR
        # tag a image to its name to check if the images exists
        try:
            docker_client.tag(name, name, force=True)
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(EXCEPTION_EXIT_CODE)
        try:
            tag = None
            if ':' in repository:
                tag = repository.split(':')[-1]
                repository = repository.replace(':%s' % tag, '')
            output = docker_client.push(repository, tag=tag, stream=True)
            utils.stream_output(output, sys.stdout)
        except StreamOutputError as e:
            if 'status 401' in e.message.lower():
                output_status = AUTH_ERROR
            else:
                print(e, file=sys.stderr)
                sys.exit(EXCEPTION_EXIT_CODE)
        except Exception as e:
            print(e.message, file=sys.stderr)
            sys.exit(EXCEPTION_EXIT_CODE)

        if output_status == NO_ERROR:
            print('')
            sys.exit()

        if output_status == AUTH_ERROR:
            print('Please login prior to push:')
            username = raw_input('Username: ')
            password = getpass.getpass()
            email = raw_input('Email: ')
            try:
                result = docker_client.login(username, password=password, email=email)
                if isinstance(result, dict):
                    print(result.get('Status', None))
            except Exception as e:
                print(e, file=sys.stderr)
                sys.exit(TUTUM_AUTH_ERROR_EXIT_CODE)
            push_to_public(repository)

    def push_to_tutum(repository):
        print('Pushing %s to Tutum private registry ...' % repository)

        user = tutum.user
        apikey = tutum.apikey
        if user is None or apikey is None:
            print('Not authorized')
            sys.exit(TUTUM_AUTH_ERROR_EXIT_CODE)

        try:
            registry = os.getenv('TUTUM_REGISTRY_URL') or 'tutum.co'
            docker_client.login(user, apikey, registry=registry)
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(TUTUM_AUTH_ERROR_EXIT_CODE)

        if repository:
            repository = filter(None, repository.split('/'))[-1]
        tag = None
        if ':' in repository:
            tag = repository.split(':')[-1]
            repository = repository.replace(':%s' % tag, '')
        repository = '%s/%s/%s' % (registry.split('//')[-1].split('/')[0], user, repository)

        if tag:
            print('Tagging %s as %s:%s ...' % (name, repository, tag))
        else:
            print('Tagging %s as %s ...' % (name, repository))

        try:
            docker_client.tag(name, repository, tag=tag, force=True)
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(EXCEPTION_EXIT_CODE)

        output = docker_client.push(repository, tag=tag, stream=True)
        try:
            utils.stream_output(output, sys.stdout)
        except docker.errors.APIError as e:
            print(e.explanation, file=sys.stderr)
            sys.exit(EXCEPTION_EXIT_CODE)
        except Exception as e:
            print(e.message, file=sys.stderr)
            sys.exit(EXCEPTION_EXIT_CODE)
        print('')

    docker_client = utils.get_docker_client()
    if public:
        push_to_public(name)
    else:
        push_to_tutum(name)


def image_rm(repositories, sync):
    has_exception = False
    for repository in repositories:
        try:
            image = tutum.Image.fetch(repository)
            result = image.delete()
            utils.sync_action(image, sync)
            if result:
                print(repository)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def image_search(text):
    try:
        docker_client = utils.get_docker_client()
        results = docker_client.search(text)
        headers = ["NAME", "DESCRIPTION", "STARS", "OFFICIAL", "TRUSTED"]
        data_list = []
        if len(results) != 0:
            for result in results:
                description = result["description"].replace("\n", "\\n")
                description = description[:80] + " [...]" if len(result["description"]) > 80 else description
                data_list.append([result["name"], description, str(result["star_count"]),
                                  u"\u2713" if result["is_official"] else "",
                                  u"\u2713" if result["is_trusted"] else ""])
        else:
            data_list.append(["", "", "", "", ""])
        utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def image_update(repositories, username, password, description, sync):
    has_exception = False
    for repository in repositories:
        try:
            image = tutum.Image.fetch(repository)
            if username is not None:
                image.username = username
            if password is not None:
                image.password = password
            if description is not None:
                image.description = description
            result = image.save()
            utils.sync_action(image, sync)
            if result:
                print(image.name)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def node_list(quiet):
    try:
        headers = ["UUID", "FQDN", "LASTSEEN", "STATUS", "CLUSTER", "DOCKER_VER"]
        node_list = tutum.Node.list()
        data_list = []
        long_uuid_list = []
        for node in node_list:
            cluster_name = node.node_cluster
            try:
                cluster_name = tutum.NodeCluster.fetch(node.node_cluster.strip("/").split("/")[-1]).name
            except:
                pass

            data_list.append([node.uuid[:8],
                              node.external_fqdn,
                              utils.get_humanize_local_datetime_from_utc_datetime_string(node.last_seen),
                              utils.add_unicode_symbol_to_state(node.state),
                              cluster_name, node.docker_version])
            long_uuid_list.append(node.uuid)
        if len(data_list) == 0:
            data_list.append(["", "", "", "", "", ""])
        if quiet:
            for uuid in long_uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def node_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            node = tutum.Utils.fetch_remote_node(identifier)
            print(json.dumps(tutum.Node.fetch(node.uuid).get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def node_rm(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            node = tutum.Utils.fetch_remote_node(identifier)
            result = node.delete()
            utils.sync_action(node, sync)
            if result:
                print(node.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def node_upgrade(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            node = tutum.Utils.fetch_remote_node(identifier)
            result = node.upgrade_docker()
            utils.sync_action(node, sync)
            if result:
                print(node.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def node_byo():
    token = ""
    try:
        json = tutum.api.http.send_request("POST", "token")
        if json:
            token = json.get("token", "")
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)

    print("Tutum lets you use your own servers as nodes to run containers. For this you have to install our agent.")
    print("Run the following command on your server:")
    print()
    print("\tcurl -Ls https://files.tutum.co/scripts/install-agent.sh | sudo -H sh -s", token)
    print()


def nodecluster_list(quiet):
    try:
        headers = ["NAME", "UUID", "REGION", "TYPE", "DEPLOYED", "STATUS", "CURRENT#NODES", "TARGET#NODES"]
        nodecluster_list = tutum.NodeCluster.list()
        data_list = []
        long_uuid_list = []
        for nodecluster in nodecluster_list:
            if quiet:
                long_uuid_list.append(nodecluster.uuid)
                continue

            node_type = nodecluster.node_type
            region = nodecluster.region
            try:
                node_type = tutum.NodeType.fetch(nodecluster.node_type.strip("/").split("api/v1/nodetype/")[-1]).label
                region = tutum.Region.fetch(nodecluster.region.strip("/").split("api/v1/region/")[-1]).label
            except Exception:
                pass

            data_list.append([nodecluster.name,
                              nodecluster.uuid[:8],
                              region,
                              node_type,
                              utils.get_humanize_local_datetime_from_utc_datetime_string(nodecluster.deployed_datetime),
                              nodecluster.state,
                              nodecluster.current_num_nodes,
                              nodecluster.target_num_nodes])
            long_uuid_list.append(nodecluster.uuid)
        if len(data_list) == 0:
            data_list.append(["", "", "", "", "", "", "", ""])
        if quiet:
            for uuid in long_uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            nodecluster = tutum.Utils.fetch_remote_nodecluster(identifier)
            print(json.dumps(tutum.NodeCluster.fetch(nodecluster.uuid).get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_show_providers(quiet):
    try:
        headers = ["NAME", "LABEL"]
        data_list = []
        name_list = []
        provider_list = tutum.Provider.list()
        for provider in provider_list:
            if quiet:
                name_list.append(provider.name)
                continue

            data_list.append([provider.name, provider.label])

        if len(data_list) == 0:
            data_list.append(["", ""])
        if quiet:
            for name in name_list:
                print(name)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_show_regions(provider):
    try:
        headers = ["NAME", "LABEL", "PROVIDER"]
        data_list = []
        region_list = tutum.Region.list()
        for region in region_list:
            provider_name = region.resource_uri.strip("/").split("/")[-2]
            if provider and provider != provider_name:
                continue
            data_list.append([region.name, region.label, provider_name])

        if len(data_list) == 0:
            data_list.append(["", "", ""])
        utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_show_types(provider, region):
    try:
        headers = ["NAME", "LABEL", "PROVIDER", "REGIONS"]
        data_list = []
        nodetype_list = tutum.NodeType.list()
        for nodetype in nodetype_list:
            provider_name = nodetype.resource_uri.strip("/").split("/")[-2]
            regions = [region_uri.strip("/").split("/")[-1] for region_uri in nodetype.regions]
            if provider and provider != provider_name:
                continue

            if region and region not in regions:
                continue
            data_list.append([nodetype.name, nodetype.label, provider_name,
                              ", ".join(regions)])

        if len(data_list) == 0:
            data_list.append(["", "", "", ""])
        utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_create(target_num_nodes, name, provider, region, nodetype, sync):
    region_uri = "/api/v1/region/%s/%s/" % (provider, region)
    nodetype_uri = "/api/v1/nodetype/%s/%s/" % (provider, nodetype)

    try:
        nodecluster = tutum.NodeCluster.create(name=name, target_num_nodes=target_num_nodes,
                                               region=region_uri, node_type=nodetype_uri)
        nodecluster.save()
        result = nodecluster.deploy()
        utils.sync_action(nodecluster, sync)
        if result:
            print(nodecluster.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_rm(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            nodecluster = tutum.Utils.fetch_remote_nodecluster(identifier)
            result = nodecluster.delete()
            utils.sync_action(nodecluster, sync)
            if result:
                print(nodecluster.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_scale(identifiers, target_num_nodes, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            nodecluster = tutum.Utils.fetch_remote_nodecluster(identifier)
            nodecluster.target_num_nodes = target_num_nodes
            result = nodecluster.save()
            utils.sync_action(nodecluster, sync)
            if result:
                print(nodecluster.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def nodecluster_upgrade(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            nodecluster = tutum.Utils.fetch_remote_nodecluster(identifier)
            result = nodecluster.upgrade_docker()
            utils.sync_action(nodecluster, sync)
            if result:
                print(nodecluster.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def tag_add(identifiers, tags):
    has_exception = False
    for identifier in identifiers:
        try:
            try:
                obj = tutum.Utils.fetch_remote_service(identifier)
            except ObjectNotFound:
                try:
                    obj = tutum.Utils.fetch_remote_nodecluster(identifier)
                except ObjectNotFound:
                    try:
                        obj = tutum.Utils.fetch_remote_node(identifier)
                    except ObjectNotFound:
                        raise ObjectNotFound(
                            "Identifier '%s' does not match any service, node or nodecluster" % identifier)

            tag = tutum.Tag.fetch(obj)
            tag.add(tags)
            tag.save()
            print(obj.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def tag_list(identifiers, quiet):
    has_exception = False

    headers = ["IDENTIFIER", "TYPE", "TAGS"]
    data_list = []
    tags_list = []
    for identifier in identifiers:
        try:
            obj = tutum.Utils.fetch_remote_service(identifier, raise_exceptions=False)
            if isinstance(obj, ObjectNotFound):
                obj = tutum.Utils.fetch_remote_nodecluster(identifier, raise_exceptions=False)
                if isinstance(obj, ObjectNotFound):
                    obj = tutum.Utils.fetch_remote_node(identifier, raise_exceptions=False)
                    if isinstance(obj, ObjectNotFound):
                        raise ObjectNotFound(
                            "Identifier '%s' does not match any service, node or nodecluster" % identifier)
                    else:
                        obj_type = 'Node'
                else:
                    obj_type = 'NodeCluster'
            else:
                obj_type = 'Service'

            tagnames = []
            for tags in tutum.Tag.fetch(obj).list():
                tagname = tags.get('name', '')
                if tagname:
                    tagnames.append(tagname)

            data_list.append([identifier, obj_type, ' '.join(tagnames)])
            tags_list.append(' '.join(tagnames))
        except Exception as e:
            if isinstance(e, ObjectNotFound):
                data_list.append([identifier, 'None', ''])
            else:
                data_list.append([identifier, '', ''])
            tags_list.append('')
            print(e, file=sys.stderr)
            has_exception = True
    if quiet:
        for tags in tags_list:
            print(tags)
    else:
        utils.tabulate_result(data_list, headers)
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def tag_rm(identifiers, tags):
    has_exception = False
    for identifier in identifiers:
        try:
            try:
                obj = tutum.Utils.fetch_remote_service(identifier)
            except ObjectNotFound:
                try:
                    obj = tutum.Utils.fetch_remote_nodecluster(identifier)
                except ObjectNotFound:
                    try:
                        obj = tutum.Utils.fetch_remote_node(identifier)
                    except ObjectNotFound:
                        raise ObjectNotFound(
                            "Identifier '%s' does not match any service, node or nodecluster" % identifier)

            tag = tutum.Tag.fetch(obj)
            for t in tags:
                try:
                    tag.delete(t)
                except Exception as e:
                    print(e, file=sys.stderr)
                    has_exception = True
            print(obj.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def tag_set(identifiers, tags):
    has_exception = False
    for identifier in identifiers:
        try:
            try:
                obj = tutum.Utils.fetch_remote_service(identifier)
            except ObjectNotFound:
                try:
                    obj = tutum.Utils.fetch_remote_nodecluster(identifier)
                except ObjectNotFound:
                    try:
                        obj = tutum.Utils.fetch_remote_node(identifier)
                    except ObjectNotFound:
                        raise ObjectNotFound(
                            "Identifier '%s' does not match any service, node or nodecluster" % identifier)

            obj.tags = []
            for t in tags:
                new_tag = {"name": t}
                if new_tag not in obj.tags:
                    obj.tags.append(new_tag)
            obj.__addchanges__("tags")
            obj.save()

            print(obj.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def volume_list(quiet):
    try:
        headers = ["UUID", "STATE", "NODE", "VOLUMEGROUP"]
        data_list = []
        uuid_list = []
        volume_list = tutum.Volume.list()
        for volume in volume_list:
            if quiet:
                uuid_list.append(volume.uuid)
                continue

            data_list.append([volume.uuid, volume.state,
                              volume.node.strip("/").split("/")[-1],
                              volume.volume_group.strip("/").split("/")[-1]])

        if len(data_list) == 0:
            data_list.append(["", "", "", ""])
        if quiet:
            for uuid in uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def volume_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            volume = tutum.Utils.fetch_remote_volume(identifier)
            print(json.dumps(volume.get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def volumegroup_list(quiet):
    try:
        headers = ["NAME", "UUID", "STATE"]
        data_list = []
        uuid_list = []
        volumegroup_list = tutum.VolumeGroup.list()
        for volumegroup in volumegroup_list:
            if quiet:
                uuid_list.append(volumegroup.uuid)
                continue

            data_list.append([volumegroup.name, volumegroup.uuid, volumegroup.state])

        if len(data_list) == 0:
            data_list.append(["", "", ""])
        if quiet:
            for uuid in uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def volumegroup_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            volumegroup = tutum.Utils.fetch_remote_volumegroup(identifier)
            print(json.dumps(volumegroup.get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def trigger_create(identifier, name, operation):
    has_exception = False
    try:
        service = tutum.Utils.fetch_remote_service(identifier)
        trigger = tutum.Trigger.fetch(service)
        trigger.add(name, operation)
        trigger.save()
        print(service.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def trigger_list(identifier, quiet):
    headers = ["UUID", "NAME", "OPERATION", "URL"]
    data_list = []
    uuid_list = []
    try:
        service = tutum.Utils.fetch_remote_service(identifier)
        trigger = tutum.Trigger.fetch(service)
        triggers = trigger.list()
        for t in triggers:
            url = tutum.domain + t.get('url', '/')[1:]
            data_list.append([t.get('uuid', '')[:8], t.get('name', ''), t.get('operation', ''), url])
            uuid_list.append(t.get('uuid', ''))
        if quiet:
            for uuid in uuid_list:
                print(uuid)
        else:
            if len(data_list) == 0:
                data_list.append(['', '', '', ''])
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)


def trigger_rm(identifier, trigger_identifiers):
    has_exception = False
    try:
        service = tutum.Utils.fetch_remote_service(identifier)
        trigger = tutum.Trigger.fetch(service)
        uuid_list = utils.get_uuids_of_trigger(trigger, trigger_identifiers)
        try:
            for uuid in uuid_list:
                trigger.delete(uuid)
                print(uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    except Exception as e:
        print(e, file=sys.stderr)
        has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_up(name, stackfile, sync):
    try:
        stack = utils.load_stack_file(name=name, stackfile=stackfile)
        stack.save()
        result = stack.start()
        utils.sync_action(stack, sync)
        if result:
            print(stack.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_create(name, stackfile, sync):
    try:
        stack = utils.load_stack_file(name=name, stackfile=stackfile)
        result = stack.save()
        utils.sync_action(stack, sync)
        if result:
            print(stack.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_inspect(identifiers):
    has_exception = False
    for identifier in identifiers:
        try:
            stack = tutum.Utils.fetch_remote_stack(identifier)
            print(json.dumps(stack.get_all_attributes(), indent=2))
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_list(quiet):
    try:
        headers = ["NAME", "UUID", "STATUS", "DEPLOYED", "DESTROYED"]
        stack_list = tutum.Stack.list()
        data_list = []
        long_uuid_list = []
        for stack in stack_list:
            data_list.append([stack.name,
                              stack.uuid[:8],
                              utils.add_unicode_symbol_to_state(stack.state),
                              utils.get_humanize_local_datetime_from_utc_datetime_string(stack.deployed_datetime),
                              utils.get_humanize_local_datetime_from_utc_datetime_string(stack.destroyed_datetime)])
            long_uuid_list.append(stack.uuid)

        if len(data_list) == 0:
            data_list.append(["", "", "", "", ""])

        if quiet:
            for uuid in long_uuid_list:
                print(uuid)
        else:
            utils.tabulate_result(data_list, headers)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_redeploy(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            stack = tutum.Utils.fetch_remote_stack(identifier)
            result = stack.redeploy()
            utils.sync_action(stack, sync)
            if result:
                print(stack.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_start(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            stack = tutum.Utils.fetch_remote_stack(identifier)
            result = stack.start()
            utils.sync_action(stack, sync)
            if result:
                print(stack.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_stop(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            stack = tutum.Utils.fetch_remote_stack(identifier)
            result = stack.stop()
            utils.sync_action(stack, sync)
            if result:
                print(stack.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_terminate(identifiers, sync):
    has_exception = False
    for identifier in identifiers:
        try:
            stack = tutum.Utils.fetch_remote_stack(identifier)
            result = stack.delete()
            utils.sync_action(stack, sync)
            if result:
                print(stack.uuid)
        except Exception as e:
            print(e, file=sys.stderr)
            has_exception = True
    if has_exception:
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_update(identifier, stackfile, sync):
    try:
        stack = utils.load_stack_file(name=None, stackfile=stackfile, stack=tutum.Utils.fetch_remote_stack(identifier))
        result = stack.save()
        utils.sync_action(stack, sync)
        if result:
            print(stack.uuid)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)


def stack_export(identifier, stackfile):
    try:
        stack = tutum.Utils.fetch_remote_stack(identifier)
        content = stack.export()
        if content:
            print(stackfile)
            if stackfile:
                with open(stackfile, 'w') as outfile:
                    outfile.write(yaml.safe_dump(content, default_flow_style=False, allow_unicode=True))
            else:
                print(yaml.safe_dump(content, default_flow_style=False, allow_unicode=True))

    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(EXCEPTION_EXIT_CODE)
