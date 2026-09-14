variable "region" {
  description = "AWS region. EFA + cluster placement groups are single-region, single-AZ."
  type        = string
  default     = "us-east-1"
}

variable "availability_zone" {
  description = <<-EOT
    Single AZ for every node. EFA traffic cannot cross an Availability Zone and a
    cluster placement group is AZ-scoped, so this is deliberately one value and not
    a list. us-east-1d is the default because `aws ec2 describe-instance-type-offerings`
    shows it is the only AZ in us-east-1 that offers every candidate type used here
    (i3en.24xlarge, i4i.32xlarge, c5n.18xlarge, p4d.24xlarge, p5.48xlarge, g6e.48xlarge).
  EOT
  type        = string
  default     = "us-east-1d"
}

variable "project_name" {
  description = "Name prefix for all resources."
  type        = string
  default     = "aerospike-lmcache-bench"
}

variable "owner" {
  description = "Owner tag value, for cost attribution in a shared account."
  type        = string
  default     = "lbauto@aerospike.com"
}

variable "vpc_cidr" {
  description = <<-EOT
    CIDR for a dedicated VPC. This account is shared (it already holds a default VPC
    at 172.31.0.0/16, an Omnistrate VPC at 10.12.0.0/16 and an AKO-peering VPC at
    10.0.0.0/16), so the benchmark gets its own non-overlapping VPC rather than
    reusing the default. That keeps teardown total and keeps the EFA security group
    from being applied to anyone else's instances.
  EOT
  type        = string
  default     = "10.240.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR of the single EFA subnet. All nodes live here."
  type        = string
  default     = "10.240.1.0/24"
}

variable "operator_cidr" {
  description = <<-EOT
    Source CIDR allowed to SSH in, as x.x.x.x/32. There is no default on purpose:
    defaulting this to 0.0.0.0/0 is how benchmark clusters end up publicly reachable.
  EOT
  type        = string
}

variable "key_name" {
  description = "Pre-existing EC2 key pair name for SSH access."
  type        = string
}

# ---------------------------------------------------------------------------
# Aerospike server nodes
# ---------------------------------------------------------------------------

variable "server_count" {
  description = "Number of Aerospike server nodes."
  type        = number
  default     = 5
}

variable "server_instance_type" {
  description = <<-EOT
    Aerospike server instance type. Must support EFA and should have local NVMe so
    the flash-resident ceiling can be measured. i3en.24xlarge: EFA, 100 Gbps,
    8x7500 GB NVMe, 96 vCPU, 768 GiB RAM.
  EOT
  type        = string
  default     = "i3en.24xlarge"
}

# ---------------------------------------------------------------------------
# CPU load-generator client (Half A -- no GPU needed)
# ---------------------------------------------------------------------------

variable "client_count" {
  description = "Number of CPU client / load-generator nodes for Half A."
  type        = number
  default     = 1
}

variable "client_instance_type" {
  description = <<-EOT
    Half A load generator. c5n.18xlarge is the cheapest EFA-capable 100 Gbps type,
    so a single client can saturate one server's NIC while costing ~1/3 of a server.
  EOT
  type        = string
  default     = "c5n.18xlarge"
}

# ---------------------------------------------------------------------------
# GPU node (Half B -- EXPENSIVE, defaults to zero, do not enable casually)
# ---------------------------------------------------------------------------

variable "gpu_count" {
  description = <<-EOT
    Number of GPU nodes for the Half B vLLM TTFT breakdown. DEFAULTS TO 0.
    Half B is roughly 2-5x the hourly cost of the entire 5-node server cluster,
    so it is a separate, explicit decision. Leave at 0 until Half A is complete.
  EOT
  type        = number
  default     = 0
}

variable "gpu_instance_type" {
  description = <<-EOT
    GPU node type for Half B. All of these support EFA and can join the cluster
    placement group. p4d.24xlarge (8xA100 40GB, ~$21.96/hr) is the cheapest type
    with both EFA and enough HBM for a realistic vLLM model; g6e.12xlarge
    (4xL40S, ~$10.49/hr) is the budget option and is enough to measure the TTFT
    *breakdown*, which is a ratio and does not need a frontier-scale model.
  EOT
  type        = string
  default     = "g6e.12xlarge"
}

# ---------------------------------------------------------------------------
# Software versions
# ---------------------------------------------------------------------------

variable "efa_installer_version" {
  description = <<-EOT
    Pinned aws-efa-installer version. 1.50.0 is the newest version present at
    https://efa-installer.amazonaws.com/ as of 2026-09-14 (verified: 1.50.0 returns
    HTTP 200, 1.51.0 returns 403). Pinned rather than using -latest.tar.gz so a
    rebuilt node is bit-identical to the node that produced the benchmark numbers.
  EOT
  type        = string
  default     = "1.50.0"
}

variable "aerospike_namespace" {
  description = "Aerospike namespace name used by the LMCache L2 adapter."
  type        = string
  default     = "lmcache"
}

variable "nat_subnet_cidr" {
  description = <<-EOT
    CIDR of the tiny public subnet that holds only the NAT gateway. No cluster
    node is ever placed here -- all EFA nodes share the single subnet defined by
    subnet_cidr, because EFA cannot cross a subnet boundary via a router.
  EOT
  type        = string
  default     = "10.240.0.0/28"
}
