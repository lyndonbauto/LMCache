# Auto-teardown Lambda for the AIE-86 benchmark cluster.
#
# Fired by a one-time EventBridge schedule. Terminates every instance and
# deletes every NAT gateway carrying the project tag, then reports what it did
# to SNS.
#
# This is a money guard, not a lifecycle manager. It deliberately does NOT try
# to be a clean `terraform destroy`: it kills the two things that bill by the
# hour (instances, NAT gateway) and leaves the VPC, subnets, security group and
# IAM role, which are free. The operator still runs `terraform destroy`
# afterwards to clear the remainder and reconcile state.
#
# It is intentionally tolerant of partial failure -- if deleting the NAT
# gateway fails, the instances are still terminated, because instances are 99%
# of the burn rate.

import os

import boto3

PROJECT_TAG = os.environ["PROJECT_TAG"]
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

ec2 = boto3.client("ec2")
sns = boto3.client("sns")


def _terminate_instances() -> list:
    """Terminate all non-terminated instances carrying the project tag.

    Returns:
        List of instance IDs that were sent a terminate call.
    """
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [PROJECT_TAG]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    ids = [
        i["InstanceId"]
        for r in resp.get("Reservations", [])
        for i in r.get("Instances", [])
    ]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
    return ids


def _delete_nat_gateways() -> list:
    """Delete all non-deleted NAT gateways carrying the project tag.

    Returns:
        List of NAT gateway IDs that were sent a delete call.
    """
    resp = ec2.describe_nat_gateways(
        Filter=[{"Name": "tag:Project", "Values": [PROJECT_TAG]}]
    )
    ids = []
    for gw in resp.get("NatGateways", []):
        if gw.get("State") in ("deleted", "deleting"):
            continue
        ec2.delete_nat_gateway(NatGatewayId=gw["NatGatewayId"])
        ids.append(gw["NatGatewayId"])
    return ids


def handler(event: dict, context: object) -> dict:
    """Entry point. Tears down billable project resources and notifies SNS.

    Args:
        event: EventBridge Scheduler event payload. Unused.
        context: Lambda context. Unused.

    Returns:
        Dict describing what was torn down.
    """
    errors = []

    try:
        instances = _terminate_instances()
    except Exception as exc:  # noqa: BLE001 - guard must not abort on one failure
        instances = []
        errors.append(f"terminate_instances: {exc}")

    try:
        nat_gateways = _delete_nat_gateways()
    except Exception as exc:  # noqa: BLE001
        nat_gateways = []
        errors.append(f"delete_nat_gateways: {exc}")

    result = {
        "project": PROJECT_TAG,
        "terminated_instances": instances,
        "deleted_nat_gateways": nat_gateways,
        "errors": errors,
    }

    if SNS_TOPIC_ARN:
        subject = f"[{PROJECT_TAG}] auto-teardown fired"
        body = (
            f"The scheduled auto-teardown for {PROJECT_TAG} has run.\n\n"
            f"Terminated instances ({len(instances)}): "
            f"{', '.join(instances) or 'none'}\n"
            f"Deleted NAT gateways ({len(nat_gateways)}): "
            f"{', '.join(nat_gateways) or 'none'}\n"
        )
        if errors:
            body += "\nERRORS (check manually, resources may still be billing):\n"
            body += "\n".join(f"  - {e}" for e in errors)
        else:
            body += (
                "\nNo errors. Run `terraform destroy` to clear the remaining "
                "free resources (VPC, subnets, security group, IAM role) and "
                "reconcile Terraform state.\n"
            )
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=body)

    return result
