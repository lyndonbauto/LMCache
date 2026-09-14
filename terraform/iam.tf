# ---------------------------------------------------------------------------
# Instance profile granting SSM Session Manager access.
#
# This is how the operator reaches the nodes. It replaces SSH-from-the-internet
# entirely: no node has a public address, so there is no inbound attack surface,
# and the stack does not consume one Elastic IP per node (the account's EIP
# quota is 5, which the naive design would have exceeded -- see network.tf).
#
# SSM traffic is outbound-initiated from the instance, so it works through the
# NAT gateway with no inbound rule.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.project_name}-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = {
    Name = "${var.project_name}-node"
  }
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.project_name}-node"
  role = aws_iam_role.node.name
}
