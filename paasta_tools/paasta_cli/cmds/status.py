#!/usr/bin/env python
"""Contains methods used by the paasta client to check the status of the service
on the PaaSTA stack"""
from ordereddict import OrderedDict
from os.path import join
import sys

from paasta_tools.marathon_tools import DEFAULT_SOA_DIR
from paasta_tools.marathon_tools import load_deployments_json
from paasta_tools.marathon_tools import list_clusters
from paasta_tools.paasta_cli.utils import execute_paasta_serviceinit_on_remote_master
from paasta_tools.paasta_cli.utils import figure_out_service_name
from paasta_tools.paasta_cli.utils import get_pipeline_url
from paasta_tools.paasta_cli.utils import lazy_choices_completer
from paasta_tools.paasta_cli.utils import list_services
from paasta_tools.paasta_cli.utils import PaastaCheckMessages
from paasta_tools.paasta_cli.utils import x_mark
from paasta_tools.utils import DEPLOY_PIPELINE_NON_DEPLOY_STEPS
from paasta_tools.utils import PaastaColors
from service_configuration_lib import read_deploy


def add_subparser(subparsers):
    status_parser = subparsers.add_parser(
        'status',
        description="PaaSTA client will attempt to deduce the SERVICE option if"
                    " none is provided.",
        help="Display the status of a Yelp service running on PaaSTA.")
    status_parser.add_argument('-v', '--verbose', action='store_true',
                               dest="verbose", default=False,
                               help="Print out more output regarding the state of the service")
    status_parser.add_argument(
        '-s', '--service',
        help='The name of the service you wish to inspect'
    ).completer = lazy_choices_completer(list_services)
    clusters_help = (
        'A comma separated list of clusters to view. Defaults to view all clusters. '
        'Try: --clusters norcal-prod,nova-prod'
    )
    status_parser.add_argument(
        '-c', '--clusters',
        help=clusters_help,
    ).completer = lazy_choices_completer(list_clusters)
    status_parser.set_defaults(command=paasta_status)


def missing_deployments_message(service_name):
    jenkins_url = PaastaColors.cyan(
        'https://jenkins.yelpcorp.com/view/services-%s' % service_name)
    message = "%s No deployments in deployments.json yet.\n  " \
              "Has Jenkins run?\n  " \
              "Check: %s" % (x_mark(), jenkins_url)
    return message


def get_deploy_info(service_name):
    deploy_file_path = join(DEFAULT_SOA_DIR, service_name, "deploy.yaml")
    deploy_info = read_deploy(deploy_file_path)
    if not deploy_info:
        print PaastaCheckMessages.DEPLOY_YAML_MISSING
        exit(1)
    return deploy_info


def get_planned_deployments(deploy_info):
    """Yield deployment environments in the form 'cluster.instance' in the order
    they appear in the deploy.yaml file for service service_name.
    :param service_name : name of the service for we wish to inspect
    :return : a series of strings of the form: 'cluster.instance', exits on
    error if deploy.yaml is not found"""
    cluster_dict = OrderedDict()

    # Store cluster names in the order in which they are read
    # Clusters map to an ordered list of instances
    for entry in deploy_info['pipeline']:
        namespace = entry['instancename']
        if namespace not in DEPLOY_PIPELINE_NON_DEPLOY_STEPS:
            cluster, instance = namespace.split('.')
            cluster_dict.setdefault(cluster, []).append(instance)

    # Yield deployment environments in the form of 'cluster.instance'
    for cluster in cluster_dict:
        for instance in cluster_dict[cluster]:
            yield "%s.%s" % (cluster, instance)


def list_deployed_clusters(pipeline, actual_deployments):
    """Returns a list of clusters that a service is deployed to given
    an input deploy pipeline and the actual deployments"""
    deployed_clusters = []
    # Get cluster.instance in the order in which they appear in deploy.yaml
    for namespace in pipeline:
        cluster_name, instance = namespace.split('.')
        if namespace in actual_deployments:
            if cluster_name not in deployed_clusters:
                deployed_clusters.append(cluster_name)
    return deployed_clusters


def get_actual_deployments(service_name):
    deployments_json = load_deployments_json(service_name, DEFAULT_SOA_DIR)
    if not deployments_json:
        sys.stderr.write("Warning: it looks like %s has not been deployed anywhere yet!" % service_name)
    # Create a dictionary of actual $service_name Jenkins deployments
    actual_deployments = {}
    for key in deployments_json:
        service, namespace = key.encode('utf8').split(':')
        if service == service_name:
            value = deployments_json[key]['docker_image'].encode('utf8')
            sha = value[value.rfind('-') + 1:]
            actual_deployments[namespace.replace('paasta-', '', 1)] = sha
    return actual_deployments


def report_status_for_cluster(service, cluster, deploy_pipeline, actual_deployments, verbose=False):
    """With a given service and cluster, prints the status of the instances
    in that cluster"""
    # Get cluster.instance in the order in which they appear in deploy.yaml
    print
    print "cluster: %s" % cluster
    for namespace in deploy_pipeline:
        cluster_in_pipeline, instance = namespace.split('.')

        if cluster_in_pipeline != cluster:
            # This function only prints things that are relevant to cluster_name
            # We skip anything not in this cluster
            continue

        # Case: service deployed to cluster.instance
        if namespace in actual_deployments:
            unformatted_instance = instance
            instance = PaastaColors.blue(instance)
            version = actual_deployments[namespace][:8]
            # TODO: Perform sanity checks once per cluster instead of for each namespace
            status = execute_paasta_serviceinit_on_remote_master('status', cluster, service, unformatted_instance,
                                                                 verbose)

        # Case: service NOT deployed to cluster.instance
        else:
            instance = PaastaColors.red(instance)
            version = 'None'
            status = None

        print '  instance: %s' % instance
        print '    Git sha:    %s' % version
        if status is not None:
            for line in status.rstrip().split('\n'):
                print '    %s' % line


def report_bogus_filters(cluster_filter, deployed_clusters):
    """Warns the user if the filter used is not even in the deployed
    list. Helps pick up typos"""
    return_string = ""
    if cluster_filter is not None:
        bogus_clusters = []
        for c in cluster_filter:
            if c not in deployed_clusters:
                bogus_clusters.append(c)
        if len(bogus_clusters) > 0:
            return_string = (
                "\n"
                "Warning: The following clusters in the filter look bogus, this service\n"
                "is not deployed to the following cluster(s):\n%s"
            ) % ",".join(bogus_clusters)
    return return_string


def report_status(service_name, deploy_pipeline, actual_deployments, cluster_filter, verbose=False):
    pipeline_url = get_pipeline_url(service_name)
    print "Pipeline: %s" % pipeline_url

    deployed_clusters = list_deployed_clusters(deploy_pipeline, actual_deployments)
    for cluster in deployed_clusters:
        if cluster_filter is None or cluster in cluster_filter:
            report_status_for_cluster(service_name, cluster, deploy_pipeline, actual_deployments, verbose)

    print report_bogus_filters(cluster_filter, deployed_clusters)


def paasta_status(args):
    """Print the status of a Yelp service running on PaaSTA.
    :param args: argparse.Namespace obj created from sys.args by paasta_cli"""
    service_name = figure_out_service_name(args)
    actual_deployments = get_actual_deployments(service_name)
    deploy_info = get_deploy_info(service_name)
    if args.clusters is not None:
        cluster_filter = args.clusters.split(",")
    else:
        cluster_filter = None

    if actual_deployments:
        deploy_pipeline = list(get_planned_deployments(deploy_info))
        report_status(service_name, deploy_pipeline, actual_deployments, cluster_filter, args.verbose)
    else:
        print missing_deployments_message(service_name)
