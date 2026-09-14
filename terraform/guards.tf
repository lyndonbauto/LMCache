# ===========================================================================
# COST GUARDS
#
# Both of these exist before any instance does. A guard added after the cluster
# is running is a guard that was missing when it mattered.
#
#   1. Scheduled auto-teardown -- a one-time EventBridge schedule that invokes
#      a Lambda which terminates the billable resources. Server-side, so it
#      survives the operator's laptop closing, losing VPN, or the session
#      ending.
#   2. AWS Budgets alarm at the approved ceiling, alerting at 50/80/100%.
# ===========================================================================

# ---------------------------------------------------------------------------
# Notification topic
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"

  tags = {
    Name = "${var.project_name}-alerts"
  }
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

  # NOTE: an email subscription is "pending confirmation" until the recipient
  # clicks the link AWS sends. Until then SNS delivers nothing. The budget
  # alarms below therefore ALSO notify the address directly via the Budgets
  # EMAIL subscriber type, which requires no confirmation -- so budget alerts
  # arrive even if nobody confirms this subscription.
}

# ---------------------------------------------------------------------------
# 1. Auto-teardown
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "teardown" {
  # Read permissions are unconditional: the Lambda has to be able to find the
  # resources before it can filter them.
  statement {
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeNatGateways",
    ]
    resources = ["*"]
  }

  # Destructive permissions are constrained to resources carrying this
  # project's tag, so a bug in the Lambda cannot reach other teams' instances
  # in this shared account.
  statement {
    effect    = "Allow"
    actions   = ["ec2:TerminateInstances"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = [var.project_name]
    }
  }

  # DeleteNatGateway does not support resource-level tag conditions, so it
  # cannot be constrained the same way. The Lambda code compensates by
  # filtering on the project tag before calling delete.
  statement {
    effect    = "Allow"
    actions   = ["ec2:DeleteNatGateway"]
    resources = ["*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role" "teardown" {
  name               = "${var.project_name}-teardown"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "teardown" {
  name   = "${var.project_name}-teardown"
  role   = aws_iam_role.teardown.id
  policy = data.aws_iam_policy_document.teardown.json
}

resource "aws_iam_role_policy_attachment" "teardown_logs" {
  role       = aws_iam_role.teardown.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "archive_file" "teardown" {
  type        = "zip"
  source_file = "${path.module}/lambda/auto_teardown.py"
  output_path = "${path.module}/.build/auto_teardown.zip"
}

resource "aws_lambda_function" "teardown" {
  function_name    = "${var.project_name}-auto-teardown"
  role             = aws_iam_role.teardown.arn
  handler          = "auto_teardown.handler"
  runtime          = "python3.12"
  timeout          = 120
  filename         = data.archive_file.teardown.output_path
  source_code_hash = data.archive_file.teardown.output_base64sha256

  environment {
    variables = {
      PROJECT_TAG   = var.project_name
      SNS_TOPIC_ARN = aws_sns_topic.alerts.arn
    }
  }

  tags = {
    Name = "${var.project_name}-auto-teardown"
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.project_name}-scheduler"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.teardown.arn
    }]
  })
}

locals {
  # Deadline is computed at apply time, not plan time. timeadd() on
  # timestamp() means the clock starts when the cluster is created.
  #
  # EventBridge Scheduler at() expressions are in UTC and must not carry a
  # timezone suffix, hence the trailing "Z" is stripped.
  teardown_deadline_utc = timeadd(timestamp(), "${var.auto_teardown_hours}h")
  teardown_at           = replace(local.teardown_deadline_utc, "Z", "")
}

resource "aws_scheduler_schedule" "teardown" {
  name                         = "${var.project_name}-auto-teardown"
  description                  = "AIE-86 cost guard: tears down the benchmark cluster at a deadline"
  schedule_expression          = "at(${local.teardown_at})"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.teardown.arn
    role_arn = aws_iam_role.scheduler.arn
  }

  # The deadline is set once, at create time. Terraform must NOT recompute it
  # on every plan -- otherwise `timestamp()` changes on each run and the
  # schedule shows as perpetually drifted, and worse, a routine `terraform
  # apply` for an unrelated change would silently extend the deadline.
  #
  # Extending is therefore a deliberate, explicit action:
  #     ./bin/extend-teardown.sh <hours>
  lifecycle {
    ignore_changes = [schedule_expression]
  }
}

# ---------------------------------------------------------------------------
# 2. Budget alarm
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "ceiling" {
  name         = "${var.project_name}-ceiling"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_ceiling_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Scoped to EC2 compute in this region rather than to the project tag.
  #
  # A tag filter would be more precise, but cost-allocation tags must be
  # activated and then take up to 24h to appear in billing data. For a
  # benchmark measured in hours, a tag-filtered budget would read $0 for the
  # entire run and never fire -- a guard that silently does nothing, which is
  # the exact failure mode this project keeps tripping over.
  #
  # Scoping to EC2/us-east-1 in a shared account means the budget may also
  # count other teams' EC2 spend. That is conservative: it alerts early rather
  # than late, which is the correct bias for a spend guard.
  cost_filter {
    name   = "Service"
    values = ["Amazon Elastic Compute Cloud - Compute"]
  }

  cost_filter {
    name   = "Region"
    values = [var.region]
  }

  dynamic "notification" {
    for_each = var.budget_alert_thresholds_pct

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
      subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
    }
  }

  # Forecast alert at 100%: warns that the run is *trending* over the ceiling
  # before it actually crosses it, which is the only alert that arrives early
  # enough to change a decision.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_sns_topic_policy" "budgets" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.alerts.arn
    }]
  })
}
