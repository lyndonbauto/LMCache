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


  # The cost guards must exist BEFORE any billable instance does. Terraform
  # would otherwise be free to create these in parallel with the schedule and
  # the budget, leaving a window -- however short -- in which a $68/hr cluster
  # is running with no deadline and no spend alarm. If the guards fail to
  # create, nothing billable gets created either.
  depends_on = [
    aws_scheduler_schedule.teardown,
    aws_budgets_budget.ceiling,
  ]


  # user_data only ever executes on first boot. Changing it on a running
  # instance therefore changes nothing functional -- but EC2 requires the
  # instance to be STOPPED to modify the attribute, so Terraform would stop and
  # start the node to apply a no-op. On i3en that destroys the ephemeral NVMe
  # and with it the entire benchmark dataset.
  #
  # Ignoring it means a corrected template still applies to freshly created
  # nodes (ignore_changes does not affect creation) while never disturbing a
  # running cluster. Bootstrap fixes for a live node are pushed via
  # ./bin/ssm-run.sh instead.
  lifecycle {
    # ami is ignored for the same reason as user_data, and it is the more
    # dangerous of the two.
    #
    # data.aws_ssm_parameter.al2023 resolves the LATEST Amazon Linux 2023 image
    # at every plan. AWS republishes that image regularly -- it changed once
    # during this very session -- and a changed AMI FORCES REPLACEMENT. The
    # result is that an apply intended to add a single GPU node silently
    # destroyed and recreated all five storage nodes and the client, losing the
    # ephemeral NVMe dataset and about an hour of cluster setup.
    #
    # Ignoring ami means existing nodes keep the image they booted with, while
    # newly created nodes still get the current one (ignore_changes does not
    # affect creation). To deliberately roll the fleet onto a new image, taint
    # or destroy the nodes explicitly.
    ignore_changes = [ami, user_data]
  }

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
    efa_bootstrap  = local.efa_bootstrap
    server_ips     = local.server_ips
    namespace      = var.aerospike_namespace
    lmcache_commit = var.lmcache_commit
  })


  # The cost guards must exist BEFORE any billable instance does. Terraform
  # would otherwise be free to create these in parallel with the schedule and
  # the budget, leaving a window -- however short -- in which a $68/hr cluster
  # is running with no deadline and no spend alarm. If the guards fail to
  # create, nothing billable gets created either.
  depends_on = [
    aws_scheduler_schedule.teardown,
    aws_budgets_budget.ceiling,
  ]


  # user_data only ever executes on first boot. Changing it on a running
  # instance therefore changes nothing functional -- but EC2 requires the
  # instance to be STOPPED to modify the attribute, so Terraform would stop and
  # start the node to apply a no-op. On i3en that destroys the ephemeral NVMe
  # and with it the entire benchmark dataset.
  #
  # Ignoring it means a corrected template still applies to freshly created
  # nodes (ignore_changes does not affect creation) while never disturbing a
  # running cluster. Bootstrap fixes for a live node are pushed via
  # ./bin/ssm-run.sh instead.
  lifecycle {
    # ami is ignored for the same reason as user_data, and it is the more
    # dangerous of the two.
    #
    # data.aws_ssm_parameter.al2023 resolves the LATEST Amazon Linux 2023 image
    # at every plan. AWS republishes that image regularly -- it changed once
    # during this very session -- and a changed AMI FORCES REPLACEMENT. The
    # result is that an apply intended to add a single GPU node silently
    # destroyed and recreated all five storage nodes and the client, losing the
    # ephemeral NVMe dataset and about an hour of cluster setup.
    #
    # Ignoring ami means existing nodes keep the image they booted with, while
    # newly created nodes still get the current one (ignore_changes does not
    # affect creation). To deliberately roll the fleet onto a new image, taint
    # or destroy the nodes explicitly.
    ignore_changes = [ami, user_data]
  }

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
    efa_bootstrap  = local.efa_bootstrap
    server_ips     = local.server_ips
    namespace      = var.aerospike_namespace
    lmcache_commit = var.lmcache_commit
  })


  # The cost guards must exist BEFORE any billable instance does. Terraform
  # would otherwise be free to create these in parallel with the schedule and
  # the budget, leaving a window -- however short -- in which a $68/hr cluster
  # is running with no deadline and no spend alarm. If the guards fail to
  # create, nothing billable gets created either.
  depends_on = [
    aws_scheduler_schedule.teardown,
    aws_budgets_budget.ceiling,
  ]


  # user_data only ever executes on first boot. Changing it on a running
  # instance therefore changes nothing functional -- but EC2 requires the
  # instance to be STOPPED to modify the attribute, so Terraform would stop and
  # start the node to apply a no-op. On i3en that destroys the ephemeral NVMe
  # and with it the entire benchmark dataset.
  #
  # Ignoring it means a corrected template still applies to freshly created
  # nodes (ignore_changes does not affect creation) while never disturbing a
  # running cluster. Bootstrap fixes for a live node are pushed via
  # ./bin/ssm-run.sh instead.
  lifecycle {
    # ami is ignored for the same reason as user_data, and it is the more
    # dangerous of the two.
    #
    # data.aws_ssm_parameter.al2023 resolves the LATEST Amazon Linux 2023 image
    # at every plan. AWS republishes that image regularly -- it changed once
    # during this very session -- and a changed AMI FORCES REPLACEMENT. The
    # result is that an apply intended to add a single GPU node silently
    # destroyed and recreated all five storage nodes and the client, losing the
    # ephemeral NVMe dataset and about an hour of cluster setup.
    #
    # Ignoring ami means existing nodes keep the image they booted with, while
    # newly created nodes still get the current one (ignore_changes does not
    # affect creation). To deliberately roll the fleet onto a new image, taint
    # or destroy the nodes explicitly.
    ignore_changes = [ami, user_data]
  }

  tags = {
    Name = "${var.project_name}-gpu-${count.index}"
    Role = "vllm-lmcache"
  }
}
