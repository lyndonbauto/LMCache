# ---------------------------------------------------------------------------
# Dedicated VPC.
#
# ALL CLUSTER NODES LIVE IN EXACTLY ONE SUBNET IN EXACTLY ONE AZ. That is not a
# simplification, it is a requirement: EFA traffic is not routable and cannot
# cross an Availability Zone, and a cluster placement group is itself AZ-scoped.
# Spreading these nodes for "availability" would silently disable RDMA.
#
# There is a second, tiny public subnet, but it holds only the NAT gateway. No
# cluster node is ever placed in it.
# ---------------------------------------------------------------------------

resource "aws_vpc" "bench" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

resource "aws_internet_gateway" "bench" {
  vpc_id = aws_vpc.bench.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

# The one and only subnet that holds cluster nodes.
resource "aws_subnet" "efa" {
  vpc_id            = aws_vpc.bench.id
  cidr_block        = var.subnet_cidr
  availability_zone = var.availability_zone

  # Public auto-assign is pointless here: attaching a pre-created EFA ENI
  # suppresses it. Outbound reachability comes from the NAT gateway instead.
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.project_name}-efa-subnet"
  }
}

# NAT-only public subnet. Deliberately holds nothing else.
resource "aws_subnet" "nat" {
  vpc_id                  = aws_vpc.bench.id
  cidr_block              = var.nat_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_name}-nat-subnet"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.bench.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.bench.id
  }

  tags = {
    Name = "${var.project_name}-rt-public"
  }
}

resource "aws_route_table_association" "nat" {
  subnet_id      = aws_subnet.nat.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# NAT gateway.
#
# Why a NAT gateway instead of giving every node a public IP:
#
# This account's "EC2-VPC Elastic IPs" quota (L-0263D0A3) is 5, with 1 already
# in use -- 4 available. A public IP per node needs 6 (7 with the GPU node), so
# the naive design fails at apply time with AddressLimitExceeded after most of
# the cluster has already been created. A NAT gateway needs exactly one EIP
# regardless of cluster size, so the stack no longer has a quota dependency.
#
# It is also the better design: no cluster node is reachable from the internet
# at all, and operator access goes through SSM Session Manager (see iam.tf).
#
# Cost is ~$0.045/hr plus $0.045/GB processed. Bootstrap pulls a few GB, so
# call it well under $2 for the whole benchmark -- noise against $58/hr.
# ---------------------------------------------------------------------------

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = {
    Name = "${var.project_name}-nat-eip"
  }
}

resource "aws_nat_gateway" "bench" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.nat.id
  depends_on    = [aws_internet_gateway.bench]

  tags = {
    Name = "${var.project_name}-nat"
  }
}

resource "aws_route_table" "efa" {
  vpc_id = aws_vpc.bench.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.bench.id
  }

  tags = {
    Name = "${var.project_name}-rt-efa"
  }
}

resource "aws_route_table_association" "efa" {
  subnet_id      = aws_subnet.efa.id
  route_table_id = aws_route_table.efa.id
}

# ---------------------------------------------------------------------------
# Cluster placement group.
#
# "cluster" strategy packs the instances onto the same high-bisection-bandwidth
# network segment, which is what makes full NIC line rate and low-tail-latency
# RDMA achievable. It is also the thing most likely to fail at apply time with
# InsufficientInstanceCapacity, because AWS must find N identical large
# instances physically close together. See the runbook for the fallback.
# ---------------------------------------------------------------------------

resource "aws_placement_group" "bench" {
  name     = "${var.project_name}-pg"
  strategy = "cluster"

  tags = {
    Name = "${var.project_name}-pg"
  }
}

# ---------------------------------------------------------------------------
# Security group.
#
# READ BEFORE EDITING -- the two self-referencing rules below are load-bearing.
# ---------------------------------------------------------------------------

resource "aws_security_group" "efa" {
  name        = "${var.project_name}-efa-sg"
  description = "EFA cluster SG: self-referencing all-traffic ingress AND egress"
  vpc_id      = aws_vpc.bench.id

  tags = {
    Name = "${var.project_name}-efa-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# !!! DO NOT DELETE -- REQUIRED BY EFA, NOT A LEFTOVER !!!
#
# EFA requires that the security group allow ALL traffic to and from ITSELF.
# Both directions. This ingress rule is the half people usually remember.
#
# Why "all protocols" rather than a port list: EFA/SRD traffic does not use TCP
# or UDP port semantics that a normal port-ranged rule can match, so it must be
# protocol "-1" with the SG's own ID as the source.
resource "aws_vpc_security_group_ingress_rule" "self_all" {
  security_group_id            = aws_security_group.efa.id
  referenced_security_group_id = aws_security_group.efa.id
  ip_protocol                  = "-1"
  description                  = "EFA: ALL inbound from self (required, do not remove)"
}

# !!! DO NOT DELETE -- THIS IS THE ONE THAT SILENTLY BREAKS EVERYTHING !!!
#
# The self-referencing EGRESS rule is the classic EFA failure. Security groups
# are stateful, so for ordinary TCP an all-outbound rule is enough and nobody
# notices this is missing. EFA is different: with this rule absent, libfabric
# will still enumerate the device, ibv_devinfo will still look healthy, and the
# connection will appear to establish -- and then RDMA writes fail or hang, with
# no log line that points at the security group. Debugging that from scratch
# costs days.
#
# A generic 0.0.0.0/0 egress rule does NOT substitute for this. AWS requires the
# egress rule to reference the security group itself for intra-SG EFA traffic.
# If a security audit or a "tighten the rules" cleanup flags this, the answer is
# that it is scoped to members of this SG only, which is exactly the blast radius
# of the cluster itself.
resource "aws_vpc_security_group_egress_rule" "self_all" {
  security_group_id            = aws_security_group.efa.id
  referenced_security_group_id = aws_security_group.efa.id
  ip_protocol                  = "-1"
  description                  = "EFA: ALL outbound to self (REQUIRED - omitting this breaks RDMA silently)"
}

# Operator SSH.
#
# Primary access is SSM Session Manager, which needs no inbound rule at all.
# This rule exists for the case where the operator adds a bastion or a VPN and
# wants plain SSH. It is scoped to a single operator IP -- never 0.0.0.0/0 --
# and because the nodes have no public addresses it is currently reachable only
# from inside the VPC or over a peering/VPN path.
resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.efa.id
  cidr_ipv4         = var.operator_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  description       = "SSH from operator CIDR"
}

# Aerospike service, fabric, heartbeat and info ports.
# 3000 service / 3001 fabric / 3002 heartbeat (mesh) / 3003 info.
# These are already covered by the self-referencing ingress rule above; they are
# declared explicitly so that the intent is documented and so the cluster still
# forms if someone narrows the blanket rule during a future hardening pass.
resource "aws_vpc_security_group_ingress_rule" "aerospike" {
  security_group_id            = aws_security_group.efa.id
  referenced_security_group_id = aws_security_group.efa.id
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3003
  description                  = "Aerospike service/fabric/heartbeat/info within cluster"
}

# Egress to the internet, needed only at bootstrap to fetch the EFA installer,
# the Aerospike packages and the LMCache dependencies.
resource "aws_vpc_security_group_egress_rule" "internet" {
  security_group_id = aws_security_group.efa.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Outbound to internet for package installation"
}
