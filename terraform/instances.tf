# ---------------------------------------------------------------------------
# AMI, resolved dynamically. Amazon Linux 2023, x86_64, kernel-default.
# Resolved through the SSM public parameter rather than an ec2:DescribeImages
# name filter, because the SSM parameter is the interface AWS actually commits
# to and it cannot accidentally match a community AMI.
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  ami_id = data.aws_ssm_parameter.al2023.value

  # Deterministic private IPs.
  #
  # The Aerospike mesh heartbeat needs every node to know its peers' addresses.
  # Letting AWS assign addresses would create a chicken-and-egg problem at
  # bootstrap (user-data would have to discover peers at runtime). Pinning the
  # addresses from the subnet CIDR means aerospike.conf can be fully rendered at
  # plan time and the config a reviewer reads is the config that runs.
  # AWS reserves the first four addresses in a subnet, so start at offset 10.
  server_ips = [for i in range(var.server_count) : cidrhost(var.subnet_cidr, 10 + i)]
  client_ips = [for i in range(var.client_count) : cidrhost(var.subnet_cidr, 100 + i)]
  gpu_ips    = [for i in range(var.gpu_count) : cidrhost(var.subnet_cidr, 200 + i)]

  # Rendered once and reused, so every node gets a byte-identical aerospike.conf.
  aerospike_conf = templatefile("${path.module}/templates/aerospike.conf.tftpl", {
    namespace  = var.aerospike_namespace
    mesh_peers = local.server_ips
  })

  efa_bootstrap = templatefile("${path.module}/templates/efa_bootstrap.sh.tftpl", {
    efa_installer_version = var.efa_installer_version
  })
}

# ---------------------------------------------------------------------------
# EFA network interfaces.
#
# interface_type = "efa" cannot be set from an inline `network_interface` block
# on aws_instance, so the ENIs are standalone resources and then attached at
# device_index 0. A consequence is that map_public_ip_on_launch no longer
# applies, hence the explicit Elastic IPs further down.
# ---------------------------------------------------------------------------

resource "aws_network_interface" "server" {
  count = var.server_count

  subnet_id       = aws_subnet.efa.id
  private_ips     = [local.server_ips[count.index]]
  security_groups = [aws_security_group.efa.id]
  interface_type  = "efa"

  tags = {
    Name = "${var.project_name}-server-${count.index}-efa"
  }
}

resource "aws_network_interface" "client" {
  count = var.client_count

  subnet_id       = aws_subnet.efa.id
  private_ips     = [local.client_ips[count.index]]
  security_groups = [aws_security_group.efa.id]
  interface_type  = "efa"

  tags = {
    Name = "${var.project_name}-client-${count.index}-efa"
  }
}

resource "aws_network_interface" "gpu" {
  count = var.gpu_count

  subnet_id       = aws_subnet.efa.id
  private_ips     = [local.gpu_ips[count.index]]
  security_groups = [aws_security_group.efa.id]
  interface_type  = "efa"

  tags = {
    Name = "${var.project_name}-gpu-${count.index}-efa"
  }
}

# ---------------------------------------------------------------------------
# Aerospike server nodes
# ---------------------------------------------------------------------------

resource "aws_instance" "server" {
  count = var.server_count

  ami                  = local.ami_id
  instance_type        = var.server_instance_type
  key_name             = var.key_name
  placement_group      = aws_placement_group.bench.name
  iam_instance_profile = aws_iam_instance_profile.node.name

  network_interface {
    network_interface_id = aws_network_interface.server[count.index].id
    device_index         = 0
  }

  root_block_device {
    volume_size = 100
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/server_user_data.sh.tftpl", {
    efa_bootstrap  = local.efa_bootstrap
    aerospike_conf = local.aerospike_conf
    node_id        = count.index + 1
    namespace      = var.aerospike_namespace
  })

  tags = {
    Name = "${var.project_name}-server-${count.index}"
    Role = "aerospike-server"
  }
}

# ---------------------------------------------------------------------------
# Half A load generator (no GPU)
# ---------------------------------------------------------------------------

resource "aws_instance" "client" {
  count = var.client_count

  ami                  = local.ami_id
  instance_type        = var.client_instance_type
  key_name             = var.key_name
  placement_group      = aws_placement_group.bench.name
  iam_instance_profile = aws_iam_instance_profile.node.name

  network_interface {
    network_interface_id = aws_network_interface.client[count.index].id
    device_index         = 0
  }

  root_block_device {
    volume_size = 200
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/client_user_data.sh.tftpl", {
    efa_bootstrap = local.efa_bootstrap
    server_ips    = local.server_ips
    namespace     = var.aerospike_namespace
  })

  tags = {
    Name = "${var.project_name}-client-${count.index}"
    Role = "bench-client"
  }
}

# ---------------------------------------------------------------------------
# Half B GPU node. gpu_count defaults to 0 -- nothing is created unless the
# human explicitly raises it.
# ---------------------------------------------------------------------------

resource "aws_instance" "gpu" {
  count = var.gpu_count

  ami                  = local.ami_id
  instance_type        = var.gpu_instance_type
  key_name             = var.key_name
  placement_group      = aws_placement_group.bench.name
  iam_instance_profile = aws_iam_instance_profile.node.name

  network_interface {
    network_interface_id = aws_network_interface.gpu[count.index].id
    device_index         = 0
  }

  root_block_device {
    volume_size = 500 # model weights are large
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/gpu_user_data.sh.tftpl", {
    efa_bootstrap = local.efa_bootstrap
    server_ips    = local.server_ips
    namespace     = var.aerospike_namespace
  })

  tags = {
    Name = "${var.project_name}-gpu-${count.index}"
    Role = "vllm-lmcache"
  }
}
