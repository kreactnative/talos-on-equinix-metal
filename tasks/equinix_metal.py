import json
import json
import os
from pprint import pprint

import yaml
from invoke import task

from tasks.ReservedVIPs import ReservedVIPs
from tasks.constellation_v01 import Cluster, VipRole, VipType
from tasks.helpers import str_presenter, get_secrets_dir, \
    get_cpem_config, get_constellation_clusters, get_constellation

yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(str, str_presenter)  # to use with safe_dump


@task()
def generate_cpem_config(ctx, cpem_config_file_name="cpem/cpem.yaml"):
    """
    Produces [secrets_dir]/cpem/cpem.yaml - 'Cloud Provider for Equinix Metal' config spec
    """
    cpem_config = get_cpem_config()
    ctx.run("mkdir -p {}".format(
        os.path.join(
            get_secrets_dir(),
            'cpem'
        )
    ), echo=True)

    command = "kubectl create -o yaml \
    --dry-run='client' secret generic -n kube-system metal-cloud-config \
    --from-literal='cloud-sa.json={}'"

    print(command.format('[REDACTED]'))
    k8s_secret = ctx.run(command.format(
        json.dumps(cpem_config)
    ), hide='stdout', echo=False)

    yaml_k8s_secret = yaml.safe_load(k8s_secret.stdout)
    del yaml_k8s_secret['metadata']['creationTimestamp']

    with open(os.path.join(get_secrets_dir(), cpem_config_file_name), 'w') as cpem_config_file:
        yaml.dump(yaml_k8s_secret, cpem_config_file)


@task()
def create_config_dirs(ctx):
    """
    Produces [secrets_dir]/[cluster_name...] config directories based of spec defined in invoke.yaml
    """
    cluster_spec = get_constellation_clusters()
    for cluster in cluster_spec:
        ctx.run("mkdir -p {}".format(os.path.join(
            get_secrets_dir(),
            cluster.name
        )), echo=True)


def _render_vip_addresses_file(key, cluster_state: dict, cluster: Cluster):
    reserved_vips = ReservedVIPs()

    for vip in cluster_state:
        pprint(vip)
        # reserved_vips.append(vip_reservation)

    # with open(ip_addresses_file_name, 'w') as ip_addresses_file:
    #     ip_addresses_file.write(reserved_vips.yaml())


def render_ip_addresses_file(ip_reservations_file_name, ip_addresses_file_name):
    with open(ip_reservations_file_name, 'r') as ip_reservations_file:
        _render_vip_addresses_file(yaml.safe_load(ip_reservations_file), ip_addresses_file_name)


def get_ip_addresses_file_path(cluster_spec: Cluster, address_role):
    return os.path.join(
        get_secrets_dir(),
        cluster_spec.name,
        "ip-{}-addresses.yaml".format(address_role)
    )


def get_ip_reservation_file_path(cluster_spec: Cluster, address_role):
    return os.path.join(
        get_secrets_dir(),
        cluster_spec.name,
        "ip-{}-reservation.yaml".format(address_role)
    )


def register_global_vip(ctx, vip: dict, tags: list):
    """
    We want to ensure that only one global_ipv4 is registered for all satellites. Following behaviour should not
    affect the management cluster (bary).

    ToDo:
        There is a bug in Metal CLI that prevents us from using the CLI in this case.
        Thankfully API endpoint works.
        https://deploy.equinix.com/developers/docs/metal/networking/global-anycast-ips/
    """
    payload = {
        "type": VipType.global_ipv4,
        "quantity": vip['count'],
        "fail_on_approval_required": "true",
        "tags": tags
    }
    result = ctx.run("curl -s -X POST "
                     "-H 'Content-Type: application/json' "
                     "-H \"X-Auth-Token: {}\" "
                     "\"https://api.equinix.com/metal/v1/projects/{}/ips\" "
                     "-d '{}'".format(
                            "${METAL_AUTH_TOKEN}",
                            "${METAL_PROJECT_ID}",
                            json.dumps(payload)
                        ), hide='stdout', echo=True).stdout

    if vip['count'] > 1:
        return [dict(yaml.safe_load(result))]
    else:
        return list(yaml.safe_load_all(result))


def get_vip_tags(address_role: VipRole, cluster: Cluster) -> list:
    """
    ToDo: Despite all the efforts to disable it
        https://github.com/kubernetes-sigs/cluster-api-provider-packet on its own registers a VIP for the control plane.
        We need one so we will use it. The tag remains defined by CAPP.
        As for the tags the 'cp' VIP is used for the Control Plane. The 'ingress' VIP will be used by the ingress.
        The 'mesh' VIP will be used by cilium as ClusterMesh endpoint.
    """
    if address_role == VipRole.cp:
        return ["cluster-api-provider-packet:cluster-id:{}".format(cluster.name)]
    else:
        return ["gocy:vip:{}".format(address_role.name), "gocy:cluster:{}".format(cluster.name)]


def register_public_vip(ctx, vip: dict, cluster: Cluster, tags: list):
    result = ctx.run("metal ip request --type {} --quantity {} --metro {} --tags '{}' -o yaml".format(
        VipType.public_ipv4,
        vip['count'],
        cluster.metro,
        ",".join(tags)
    ), hide='stdout', echo=True).stdout
    if vip['count'] > 1:
        return [dict(yaml.safe_load(result))]
    else:
        return list(yaml.safe_load_all(result))


def render_existing_vips(ctx, project_vips_file_path):
    with open(project_vips_file_path) as project_vips_file:
        project_vips = yaml.safe_load(project_vips_file)

    existing_vip_key = 'existing_vip_key'
    constellation_spec = get_constellation_clusters()
    global_vip = None

    for cluster_spec in constellation_spec:
        cluster_state = cluster_spec.dict()
        for vip_state in cluster_state['vips']:
            vip_tags = get_vip_tags(vip_state['role'], cluster_spec)
            vip_state[existing_vip_key] = list()
            for project_vip in project_vips:
                if 'tags' in project_vip and project_vip.get('tags') == vip_tags:
                    if project_vip['type'] == vip_state['vipType']:
                        # if ((project_vip['type'] == vip_state['vipType'] == str(VipType.global_ipv4))
                        #         or (project_vip['type'] == vip_state['vipType'] == str(VipType.public_ipv4)
                        #             and 'metro' in project_vip
                        #             and project_vip['metro']['code'] == cluster_spec.metro)):
                        # If we are missing VIPs mark the spot
                        if vip_state['vipType'] == VipType.global_ipv4:
                            if global_vip is None:
                                global_vip = project_vip

                            vip_state[existing_vip_key].append(global_vip)
                        else:
                            vip_state[existing_vip_key].append(project_vip)

        for vip_state in cluster_state['vips']:
            vip_tags = get_vip_tags(vip_state['role'], cluster_spec)
            if len(vip_state[existing_vip_key]) == 0:
                # Register missing VIPs
                vip_state[existing_vip_key] = list()
                if vip_state['vipType'] == VipType.public_ipv4:
                    vip_state[existing_vip_key].extend(
                        register_public_vip(ctx, vip_state, cluster_spec, vip_tags)
                    )
                else:
                    if global_vip is None:
                        global_vip = register_global_vip(ctx, vip_state, vip_tags)

                    vip_state[existing_vip_key].extend(global_vip)

        _render_vip_addresses_file(
            existing_vip_key,
            cluster_state,
            cluster_spec)


def register_vip(ctx, cluster: Cluster, project_vips_file_path,
                 address_role: VipRole, address_type: VipType, address_count: int):
    cluster_metro = cluster.metro

    ip_reservations_file_name = get_ip_reservation_file_path(cluster, address_role)
    ip_addresses_file_name = get_ip_addresses_file_path(cluster, address_role)
    # ToDo: Despite all the efforts to disable it
    #   https://github.com/kubernetes-sigs/cluster-api-provider-packet on its own registers a VIP for the control plane.
    #   We need one so we will use it. The tag remains defined by CAPP.
    if address_role == VipRole.cp:
        vip_tags = ["cluster-api-provider-packet:cluster-id:{}".format(cluster.name)]
    else:
        vip_tags = ["gocy:vip:{}".format(address_role.name), "gocy:cluster:{}".format(cluster.name)]

    if os.path.isfile(ip_reservations_file_name):
        render_ip_addresses_file(ip_reservations_file_name, ip_addresses_file_name)
        return

    with open(project_vips_file_path, 'r') as all_ips_file:
        no_reservations = False
        # ToDo: VIPs are sourced in two ways. This could be solved in more elegant and resilient way...
        #   1) Initial run, where VIPs are not yet registered on metal platform. Therefore we need to
        #       'metal ip request' them. Grab the output, parse and produce ip_addresses_file_name
        #   2) Subsequent run, where VIPs are registered on metal platform. VIPs are present in 'project-ips.yaml'
        #       together with all other IPS for a given metal project. We need to filter the ones needed for this
        #       particular constellation & cluster.
        for ip_spec in yaml.safe_load(all_ips_file):
            if (ip_spec['global_ip'] or ('metro' in ip_spec and ip_spec['metro']['code'] == cluster_metro)) \
                    and 'tags' in ip_spec and ip_spec.get('tags') == vip_tags:
                _render_vip_addresses_file(ip_spec, ip_addresses_file_name)
                no_reservations = False
                break
            else:
                no_reservations = True

        if no_reservations:
            if address_type == 'public_ipv4':
                ctx.run("metal ip request --type {} --quantity {} --metro {} --tags '{}' -o yaml > {}".format(
                    address_type,
                    address_count,
                    cluster_metro,
                    ",".join(vip_tags),
                    ip_reservations_file_name
                ), echo=True)
            elif address_type == 'global_ipv4':
                register_global_vip(ctx, cluster, vip_tags, ip_reservations_file_name)
            else:
                print("Unsupported address_type: " + address_type)

            render_ip_addresses_file(ip_reservations_file_name, ip_addresses_file_name)


@task(create_config_dirs)
def register_vips(ctx, project_vips_file_name='project-ips.yaml'):
    """
    Registers VIPs as per constellation spec in ~/.gocy/[constellation_name].constellation.yaml
    """
    project_vips_file_path = os.path.join(get_secrets_dir(), project_vips_file_name)
    ctx.run("metal ip get -o yaml > {}".format(project_vips_file_path), echo=True)

    render_existing_vips(ctx, project_vips_file_path)


@task()
def list_facilities(ctx):
    """
    Wrapper for 'metal facilities get'
    """
    ctx.run('metal facilities get', echo=True)


@task()
def check_capacity(ctx):
    """
    Check device capacity for clusters specified in invoke.yaml
    """
    nodes_total = dict()
    constellation = get_constellation()
    bary_metro = constellation.bary.metro
    nodes_total[bary_metro] = dict()
    bary_nodes = constellation.bary.control_nodes
    bary_nodes.extend(constellation.bary.worker_nodes)

    for node in bary_nodes:
        if node.plan not in nodes_total[bary_metro]:
            nodes_total[bary_metro][node.plan] = node.count
        else:
            nodes_total[bary_metro][node.plan] = nodes_total[bary_metro][node.plan] + node.count

    for satellite in constellation.satellites:
        if satellite.metro not in nodes_total:
            nodes_total[satellite.metro] = dict()

        satellite_nodes = satellite.worker_nodes
        satellite_nodes.extend(satellite.control_nodes)
        for node in satellite_nodes:
            if node.plan not in nodes_total[satellite.metro]:
                nodes_total[satellite.metro][node.plan] = node.count
            else:
                nodes_total[satellite.metro][node.plan] = nodes_total[satellite.metro][node.plan] + node.count

    for metro in nodes_total:
        for node_type, count in nodes_total[metro].items():
            ctx.run("metal capacity check --metros {} --plans {} --quantity {}".format(
                metro, node_type, count
            ), echo=True)
