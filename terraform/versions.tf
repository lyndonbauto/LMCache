# Pinned so that a `terraform plan` reviewed today produces the same resources
# when the human green-lights `terraform apply` days later.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project_name
      Jira      = "AIE-86"
      Epic      = "AIE-85"
      ManagedBy = "terraform"
      Owner     = var.owner
      # Cost guardrail: everything this stack creates carries these tags so the
      # whole benchmark can be found (and destroyed) with a single tag filter.
    }
  }
}
