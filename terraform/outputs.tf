output "placement_group_name" {
  description = "Cluster placement group holding every node."
  value       = aws_placement_group.bench.name
}

output "availability_zone" {
  description = "The single AZ. All EFA nodes must share it."
  value       = aws_subnet.efa.availability_zone
}

output "server_private_ips" {
  description = "Aerospike server private IPs (also the mesh-seed peer list)."
  value       = local.server_ips
}

output "server_public_ips" {
  description = <<-EOT
    Intentionally empty. No cluster node has a public address: operator access is
    via SSM Session Manager and outbound is via the NAT gateway. The key exists so
    that tooling expecting this output does not break, and so the absence is
    explicit rather than looking like an oversight. See docs/findings.md.
  EOT
  value       = []
}

output "client_private_ips" {
  description = "Half A load-generator private IPs."
  value       = local.client_ips
}

output "client_public_ips" {
  description = "Intentionally empty -- see server_public_ips."
  value       = []
}

output "gpu_private_ips" {
  description = "Half B GPU node private IPs. Empty unless gpu_count > 0."
  value       = local.gpu_ips
}

output "gpu_public_ips" {
  description = "Intentionally empty -- see server_public_ips."
  value       = []
}

output "aerospike_seed_hosts" {
  description = "Value for LMCACHE_AEROSPIKE_HOSTS / the adapter's `hosts` field."
  value       = join(",", [for ip in local.server_ips : "${ip}:3000"])
}

output "nat_gateway_public_ip" {
  description = "Egress address of the whole cluster. The only public IP in the stack."
  value       = aws_eip.nat.public_ip
}

output "ssm_session_examples" {
  description = "Ready-to-paste SSM commands for reaching each node."
  value = {
    for idx, inst in aws_instance.server :
    "server-${idx}" => "aws ssm start-session --target ${inst.id}"
  }
}

output "security_group_id" {
  description = "EFA security group. Carries the self-referencing ingress AND egress rules."
  value       = aws_security_group.efa.id
}

output "estimated_hourly_cost_usd" {
  description = <<-EOT
    Rough on-demand cost of what is currently configured, from us-east-1 Linux
    list prices captured 2026-09-14. Excludes EBS, EIP and data transfer, which
    are small relative to the instances.
  EOT
  value = format(
    "%.2f USD/hr (%d x %s server + %d x %s client + %d x %s gpu)",
    var.server_count * lookup(local.hourly_price, var.server_instance_type, 0)
    + var.client_count * lookup(local.hourly_price, var.client_instance_type, 0)
    + var.gpu_count * lookup(local.hourly_price, var.gpu_instance_type, 0),
    var.server_count, var.server_instance_type,
    var.client_count, var.client_instance_type,
    var.gpu_count, var.gpu_instance_type,
  )
}

locals {
  # us-east-1, Linux, shared tenancy, on-demand. Retrieved via the AWS Pricing
  # API on 2026-09-14. Hardcoded rather than queried at plan time so that a
  # `terraform plan` does not depend on pricing:GetProducts permissions.
  hourly_price = {
    "i3en.24xlarge" = 10.848
    "i4i.32xlarge"  = 10.9824
    "c5n.18xlarge"  = 3.888
    "c5n.4xlarge"   = 0.864
    "c6a.48xlarge"  = 7.344
    "m6a.48xlarge"  = 8.2944
    "r6a.48xlarge"  = 10.8864
    "p4d.24xlarge"  = 21.9576
    "p5.48xlarge"   = 55.04
    "g6e.12xlarge"  = 10.4926
    "g6e.48xlarge"  = 30.1312
  }
}
