import base64
import json
import os
from typing import Any

import jinja2
import yaml

from tasks.models.ClusterNodes import ClusterNodes
from tasks.models.ConstellationSpecV01 import Constellation, Cluster, VipRole
from tasks.models.Defaults import CONSTELLATION_FILE_SUFFIX
from tasks.models.ReservedVIPs import ReservedVIPs


def str_presenter(dumper, data):
    """
    configures yaml for dumping multiline strings
    Ref: https://stackoverflow.com/questions/8640959/how-can-i-control-what-scalar-form-pyyaml-uses-for-my-data
    """
    lines = data.splitlines()
    if len(lines) > 1:  # check for multiline string
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)


def get_cluster_spec_from_context(ctx) -> Cluster:
    context = ctx.run("kubectl config current-context", hide='stdout', echo=True).stdout
    for cluster_spec in get_constellation_clusters():
        if cluster_spec.name in context:
            return cluster_spec

    print("k8s context: '{}' not in constellation".format(context.strip()))


def get_config_dir(default_config_dir_name=".gocy"):
    default_root = os.environ.get('GOCY_DEFAULT_ROOT', None)
    if default_root is not None:
        return default_root

    return os.path.join(
        os.path.expanduser('~'),
        default_config_dir_name
    )


def get_secrets_file_name(name='secrets.yaml'):
    return os.path.join(get_config_dir(), name)


def get_secrets() -> dict:
    with open(get_secrets_file_name()) as secrets_file:
        return dict(yaml.safe_load(secrets_file))


def get_secret_envs(secrets: dict = None) -> list:
    if secrets is not None:
        return secrets['env']

    return get_secrets()['env']


def get_cpem_config():
    return {
        'apiKey': os.environ.get('PACKET_API_KEY'),
        'projectID': os.environ.get('PROJECT_ID'),
        'eipTag': '',
        'eipHealthCheckUseHostIP': True
    }


def get_cpem_config_yaml() -> str:
    return base64.b64encode(
        json.dumps(
            get_cpem_config()
        ).encode('utf-8')
    ).decode('utf-8')


def get_file_content_as_b64(filename):
    with open(filename, 'rb') as file:
        return base64.b64encode(file.read()).decode('utf-8')


def get_vips(cluster_spec: Cluster, role: VipRole) -> ReservedVIPs:
    with open(os.path.join(
            get_secrets_dir(),
            cluster_spec.name,
            'ip-{}-addresses.yaml'.format(role)
    ), 'r') as cp_address:
        return ReservedVIPs.parse_raw(cp_address.read())


def get_cp_vip_address(cluster_spec):
    return get_vips(cluster_spec, VipRole.cp).public_ipv4[0]


def get_argo_infra_namespace_name():
    return 'argo-infra'


def get_constellation_context_file_name(name="ccontext"):
    return os.path.join(get_config_dir(), name)


def get_ccontext(default_ccontext='jupiter'):
    try:
        with open(get_constellation_context_file_name()) as cc_file:
            ccontext = cc_file.read()
            if ccontext == '':
                raise OSError
            return ccontext
    except OSError:
        return default_ccontext


def get_secrets_dir():
    return os.path.join(
        get_config_dir(),
        get_ccontext()
    )


def get_constellation(name=None) -> Constellation:
    if name is None:
        name = get_ccontext()

    with open(os.path.join(get_config_dir(), name + CONSTELLATION_FILE_SUFFIX)) as constellation_file:
        return Constellation.parse_raw(constellation_file.read())


def get_constellation_clusters(constellation: Constellation = None) -> list[Cluster]:
    clusters = list()
    if constellation is None:
        constellation = get_constellation()

    clusters.append(constellation.bary)
    clusters.extend(constellation.satellites)
    return clusters


def get_nodes_ips(ctx, talosconfig_file_name='talosconfig') -> ClusterNodes:

    cluster_spec = get_cluster_spec_from_context(ctx)
    cluster_cfg_dir = os.path.join(get_secrets_dir(), cluster_spec.name)

    with open(os.path.join(cluster_cfg_dir, talosconfig_file_name), 'r') as talosconfig_file:
        talosconfig = yaml.safe_load(talosconfig_file)

    nodes_raw = ctx.run("kubectl get nodes -o yaml", hide='stdout', echo=True).stdout
    cluster_nodes = ClusterNodes()
    cp_vip = get_cp_vip_address(cluster_spec)
    for node in yaml.safe_load(nodes_raw)['items']:
        node_addresses = node['status']['addresses']
        node_addresses = list(filter(lambda address: address['type'] == 'ExternalIP', node_addresses))
        node_addresses = list(map(lambda address: address['address'], node_addresses))

        if cp_vip in node_addresses:
            node_addresses.remove(cp_vip)

        if 'node-role.kubernetes.io/control-plane' in node['metadata']['labels']:
            cluster_nodes.control_plane.extend(node_addresses)
        else:
            cluster_nodes.machines.extend(node_addresses)

    talosconfig_addresses = talosconfig['contexts'][cluster_spec.name]['nodes']
    try:
        talosconfig_addresses.remove(cp_vip)
    except ValueError:
        pass

    # from pprint import pprint
    # print("#### node_patch_data")
    # pprint(nodes)
    # print("#### talosconfig")
    # pprint(talosconfig_addresses)
    # return

    if len(set(cluster_nodes.all()) - set(talosconfig_addresses)) > 0:
        raise Exception("Node list returned by kubectl is out of sync with your talosconfig!")

    return cluster_nodes


def get_jinja():
    return jinja2.Environment(undefined=jinja2.StrictUndefined)


def get_fqdn(name: Any, secrets: dict, cluster: Cluster):

    if type(name) == list:
        _name = ".".join(name)
    else:
        _name = name

    return "{}.{}.{}".format(
        _name,
        cluster.domain_prefix,
        secrets['env']['GOCY_DOMAIN']
    )


def user_confirmed(msg=None) -> bool:
    if msg is None:
        msg = 'Continue ?'

    msg += ' [y/N] '

    user_input = input(msg)
    return user_input.strip().lower() == 'y'
