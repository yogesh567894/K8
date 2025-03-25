import requests
import pandas as pd
import time
import datetime
from kubernetes import client, config
from dateutil import parser
import os

# Configuration - Updated for Minikube
PROMETHEUS_URL = 'http://localhost:8081'  # Ensure this matches your Prometheus port-forwarding setup


  # Your Minikube Prometheus URL
NAMESPACE = 'monitoring'  # Replace 'otel-demo' with 'monitoring'
  # Default namespace in Minikube - change if you're using a different one
OUTPUT_FILE = 'pod_metrics.csv'
SLEEP_INTERVAL = 5  # Time in seconds between data fetches

# List of pod names to exclude - Updated for Minikube
EXCLUDE_POD_NAMES = [
    "storage-provisioner", 
    "metrics-server",
    "kube-proxy",
    "coredns", 
    '''
    "prometheus-alertmanager",
    "prometheus-kube-state-metrics",
    "prometheus-prometheus-node-exporter",
    "prometheus-prometheus-pushgateway",
    "prometheus-server"'''
]

# Initialize Kubernetes client
config.load_kube_config()
v1 = client.CoreV1Api()

# Function to check if a pod should be excluded
def should_exclude_pod(pod_name):
    return any(excluded in pod_name for excluded in EXCLUDE_POD_NAMES)

# Function to get pod status and additional details
def get_pod_status(pod_name, namespace):
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        status = pod.status.phase

        # Initialize variables for restart count and reasons
        restarts = 0
        reason = status if pod.status.reason is None else pod.status.reason
        ready_containers = sum(1 for c in pod.status.container_statuses if c.ready) if pod.status.container_statuses else 0
        total_containers = len(pod.spec.containers)

        # Check init containers
        for container in pod.status.init_container_statuses or []:
            restarts += container.restart_count
            if container.state.terminated and container.state.terminated.exit_code != 0:
                # Provide more detailed reason if available
                reason = f"Init: {container.state.terminated.reason or container.state.terminated.exit_code}"
                break

        # Check regular containers if init containers are fine
        if pod.status.init_container_statuses is None or all(c.state.terminated and c.state.terminated.exit_code == 0 for c in pod.status.init_container_statuses):
            for container in pod.status.container_statuses or []:
                restarts += container.restart_count
                if container.state.waiting:
                    reason = container.state.waiting.reason
                elif container.state.terminated:
                    reason = container.state.terminated.reason or container.state.terminated.exit_code

        return status, reason, restarts, ready_containers, total_containers, None  # Additional details included
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return 'NotFound', None, 0, 0, 0, f'Pod {pod_name} not found'
        else:
            return 'Error', None, 0, 0, 0, str(e)
    except Exception as e:
        return 'Unknown', None, 0, 0, 0, str(e)

# Function to get the node name for a pod
def get_pod_node_name(pod_name, namespace):
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        return pod.spec.node_name
    except Exception as e:
        print(f"Error getting node for pod {pod_name}: {e}")
        return 'Unknown'

def get_event_timestamp(event):
    """Get the most relevant timestamp from the event."""
    if event.last_timestamp:
        return event.last_timestamp
    if event.event_time:
        return event.event_time
    return event.first_timestamp

# Function to get the latest event details for a pod
def get_latest_pod_event(pod_name, namespace):
    try:
        events = v1.list_namespaced_event(namespace, field_selector=f"involvedObject.name={pod_name}")
        valid_events = [event for event in events.items if event.last_timestamp]
        if valid_events:
            latest_event = sorted(valid_events, key=lambda x: x.last_timestamp, reverse=True)[0]
            event_age = datetime.datetime.now(datetime.timezone.utc) - latest_event.last_timestamp
            event_age_str = str(event_age).split('.')[0]  # Convert to string and remove microseconds

            return {
                'Pod Event Type': latest_event.type,
                'Pod Event Reason': latest_event.reason,
                'Pod Event Age': event_age_str,
                'Pod Event Source': latest_event.source.component,
                'Pod Event Message': latest_event.message
            }
        return {
            'Pod Event Type': 'No recent events',
            'Pod Event Reason': 'N/A',
            'Pod Event Age': 'N/A',
            'Pod Event Source': 'N/A',
            'Pod Event Message': 'N/A'
        }
    except Exception as e:
        print(f"Error getting events for pod {pod_name}: {e}")
        return {
            'Pod Event Type': 'Error',
            'Pod Event Reason': 'Unknown',
            'Pod Event Age': 'Unknown',
            'Pod Event Source': 'Unknown',
            'Pod Event Message': 'Unknown'
        }

def get_latest_event_details_node(node_name):
    try:
        events = v1.list_event_for_all_namespaces(field_selector=f"involvedObject.kind=Node,involvedObject.name={node_name}")
        valid_events = [event for event in events.items if get_event_timestamp(event)]
        if valid_events:
            latest_event = sorted(valid_events, key=lambda x: get_event_timestamp(x), reverse=True)[0]
            event_timestamp = get_event_timestamp(latest_event)

            # Check if event_timestamp is already a datetime object
            if isinstance(event_timestamp, str):
                event_timestamp = parser.parse(event_timestamp)
            event_age = datetime.datetime.now(datetime.timezone.utc) - event_timestamp
            event_age_str = str(event_age).split('.')[0]  # Convert to string and remove microseconds
            
            return {
                'Node Name': node_name,
                'Event Reason': latest_event.reason,
                'Event Age': event_age_str,
                'Event Source': latest_event.source.component,
                'Event Message': latest_event.message
            }
        return {
            'Node Name': node_name,
            'Event Reason': 'No recent events',
            'Event Age': 'N/A',
            'Event Source': 'N/A',
            'Event Message': 'N/A'
        }
    except Exception as e:
        print(f"Error getting events for node {node_name}: {e}")
        return {
            'Node Name': node_name,
            'Event Reason': 'Unknown',
            'Event Age': 'Unknown',
            'Event Source': 'Unknown',
            'Event Message': 'Unknown'
        }

# Function to get the latest event reason for a pod
def get_latest_event_reason(pod_name, namespace):
    try:
        events = v1.list_namespaced_event(namespace, field_selector=f"involvedObject.name={pod_name}")
        # Filter out events with None last_timestamp and sort the rest
        valid_events = [event for event in events.items if event.last_timestamp]
        if valid_events:
            latest_event = sorted(valid_events, key=lambda x: x.last_timestamp, reverse=True)[0]
            return latest_event.reason
        return 'No recent events'
    except Exception as e:
        print(f"Error getting events for pod {pod_name}: {e}")
        return 'Unknown'

def get_last_log_entry(pod_name, namespace):
    try:
        logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=1)
        return logs if logs else "No logs"
    except Exception as e:
        print(f"Error getting logs for pod {pod_name}: {e}")
        return "Log retrieval error"

# Function to query Prometheus - Enhanced for Minikube with error handling
def query_prometheus(query):
    try:
        # Updated path for Prometheus 2.x API
        response = requests.get(f'{PROMETHEUS_URL}/api/v1/query', params={'query': query}, timeout=10)
        response.raise_for_status()
        results = response.json()['data']['result']
        
        # Handle potential differences in Prometheus response format
        pod_metrics = {}
        for item in results:
            # Try to get pod name from different possible locations in metrics
            pod_name = None
            if 'pod' in item['metric']:
                pod_name = item['metric']['pod']
            elif 'pod_name' in item['metric']:
                pod_name = item['metric']['pod_name']
            
            if pod_name:
                pod_metrics[pod_name] = float(item['value'][1])
        
        return pod_metrics
    except requests.exceptions.HTTPError as errh:
        print(f"Http Error: {errh}")
    except requests.exceptions.ConnectionError as errc:
        print(f"Error Connecting: {errc}")
    except requests.exceptions.Timeout as errt:
        print(f"Timeout Error: {errt}")
    except requests.exceptions.RequestException as err:
        print(f"Request Exception: {err}")
    except Exception as e:
        print(f"Unexpected error in query_prometheus: {str(e)}")
    return {}

# Function to calculate memory usage percentage
def calculate_percentage(usage, limit):
    return (usage / limit) * 100 if limit > 0 else 'N/A'

# Dictionary to keep track of the last known state of each pod
last_known_pod_states = {}

# Main loop
while True:
    try:
        print(f"Fetching data for pods in namespace {NAMESPACE}")

        # Fetch current state of all pods in the namespace
        current_pods = v1.list_namespaced_pod(namespace=NAMESPACE)
        current_pod_states = {pod.metadata.name: pod.status.phase for pod in current_pods.items if not should_exclude_pod(pod.metadata.name)}

        # Prometheus queries - Adapted for Minikube
        cpu_usage_query = f"100 * sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace=\"{NAMESPACE}\"}}[5m]))"
        memory_usage_query = f"sum by (pod) (container_memory_working_set_bytes{{namespace=\"{NAMESPACE}\"}} / 1024 / 1024)"  # Convert bytes to MiB
        memory_limit_query = f"sum by (pod) (kube_pod_container_resource_limits{{resource=\"memory\", namespace=\"{NAMESPACE}\"}} / 1024 / 1024)"  # Convert bytes to MiB
        # Network traffic queries - Adapted for Minikube
        network_traffic_query = f"sum by (pod) (rate(container_network_receive_bytes_total{{namespace=\"{NAMESPACE}\"}}[5m]) + rate(container_network_transmit_bytes_total{{namespace=\"{NAMESPACE}\"}}[5m]))"
        network_receive_query = f"sum by (pod) (rate(container_network_receive_bytes_total{{namespace=\"{NAMESPACE}\"}}[5m]))"
        network_transmit_query = f"sum by (pod) (rate(container_network_transmit_bytes_total{{namespace=\"{NAMESPACE}\"}}[5m]))"
        network_receive_errors_query = f"sum by (pod) (rate(container_network_receive_errors_total{{namespace=\"{NAMESPACE}\"}}[5m]))"
        network_transmit_errors_query = f"sum by (pod) (rate(container_network_transmit_errors_total{{namespace=\"{NAMESPACE}\"}}[5m]))"

        # Fetch data from Prometheus
        cpu_usage_data = query_prometheus(cpu_usage_query)
        memory_usage_data = query_prometheus(memory_usage_query)
        memory_limit_data = query_prometheus(memory_limit_query)
        network_traffic_data = query_prometheus(network_traffic_query)
        network_receive_data = query_prometheus(network_receive_query)
        network_transmit_data = query_prometheus(network_transmit_query)
        network_receive_errors_data = query_prometheus(network_receive_errors_query)
        network_transmit_errors_data = query_prometheus(network_transmit_errors_query)

        # Check for changes in pod states
        for pod_name, current_status in current_pod_states.items():
            previous_status = last_known_pod_states.get(pod_name)
            if previous_status != current_status:
                print(f"Status change detected in pod {pod_name}: {previous_status} -> {current_status}")

            # Update the last known state
            last_known_pod_states[pod_name] = current_status

        # Remove entries for pods that no longer exist
        for pod_name in list(last_known_pod_states.keys()):
            if pod_name not in current_pod_states:
                del last_known_pod_states[pod_name]
                print(f"Pod {pod_name} no longer exists")

        # Prepare data for CSV
        data = []
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Combine all pod names from metrics and current pods
        all_pods = set(current_pod_states.keys())
        if memory_usage_data:
            all_pods.update(memory_usage_data.keys())
        if memory_limit_data:
            all_pods.update(memory_limit_data.keys())
        if cpu_usage_data:
            all_pods.update(cpu_usage_data.keys())
        
        for pod in all_pods:
            if should_exclude_pod(pod):
                continue  # Skip this pod if it matches the exclude list
                
            memory_usage_percentage = calculate_percentage(memory_usage_data.get(pod, 0), memory_limit_data.get(pod, 0))
            event_reason = get_latest_event_reason(pod, NAMESPACE)
            node_name = get_pod_node_name(pod, NAMESPACE)
            last_log_entry = get_last_log_entry(pod, NAMESPACE)
            status, reason, restarts, ready_containers, total_containers, error_message = get_pod_status(pod, NAMESPACE)
            latest_pod_event_details = get_latest_pod_event(pod, NAMESPACE)
            latest_event_node_details = get_latest_event_details_node(node_name)
            
            data.append({
                'Timestamp': timestamp,
                'Pod Name': pod,
                'CPU Usage (%)': cpu_usage_data.get(pod, 'N/A'),
                'Memory Usage (%)': memory_usage_percentage,
                'Network Traffic (B/s)': network_traffic_data.get(pod, 'N/A'),
                'Network Receive (B/s)': network_receive_data.get(pod, 'N/A'),
                'Network Transmit (B/s)': network_transmit_data.get(pod, 'N/A'),
                'Network Receive Errors': network_receive_errors_data.get(pod, 'N/A'),
                'Network Transmit Errors': network_transmit_errors_data.get(pod, 'N/A'),
                'Last Log Entry': last_log_entry,
                'Pod Status': status,
                'Pod Reason': reason,
                'Pod Restarts': restarts,
                'Ready Containers': ready_containers,
                'Total Containers': total_containers,
                'Error Message': error_message,
                'Latest Event Reason': event_reason,
                **latest_pod_event_details,
                **latest_event_node_details
            })

        # Create DataFrame
        df = pd.DataFrame(data)

        # Check if the file exists to decide on writing the header
        if not os.path.isfile(OUTPUT_FILE):
            df.to_csv(OUTPUT_FILE, index=False, mode='w', header=True)
        else:
            df.to_csv(OUTPUT_FILE, index=False, mode='a', header=False)

        print(f"Data written to {OUTPUT_FILE}")

        time.sleep(SLEEP_INTERVAL)

    except KeyboardInterrupt:
        print("Script interrupted, exiting.")
        break
    except Exception as e:
        print(f"An error occurred: {e}")
        time.sleep(SLEEP_INTERVAL)  # Continue trying even if there's an error
