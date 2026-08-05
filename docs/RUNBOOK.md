# Runbook

Operational playbook for when this project is running somewhere real (staging/production). Mostly placeholder until there's an actual deployment to operate — fill in each section as that becomes true, and delete any section that ends up not applying.

## Overview

- **What this service does in one sentence**: <!-- fill in -->
- **Where it runs**: <!-- e.g. cloud provider, region, hosting platform -->
- **Who's on call / how to reach them**: <!-- e.g. PagerDuty rotation, Slack channel -->

## Health checks

- **How to tell it's healthy**: <!-- health endpoint, dashboard link, expected metrics range -->
- **Dashboards**: <!-- links to monitoring/observability dashboards -->
- **Logs**: <!-- where logs live and how to query them -->

## Deploying

- **How a deploy happens**: <!-- CI/CD pipeline, manual steps, approval gates -->
- **How to roll back**: <!-- exact steps/commands, not just "revert the PR" -->
- **Pre-deploy checklist**: <!-- migrations to run first, feature flags to check, etc. -->

## Common incidents

<!-- One subsection per recurring failure mode, added as they're discovered. Each should be a real incident that happened, not a hypothetical. -->

### Example: `<symptom>`

- **Symptom**: What an operator observes (error rate spike, specific error message, timeout).
- **Likely cause(s)**:
- **Diagnosis steps**:
- **Fix**:
- **Follow-up**: Link to the postmortem or issue, if one exists.

## Escalation

- **When to page someone vs. handle it yourself**:
- **Who owns which area**: <!-- can point to CODEOWNERS instead of duplicating -->

## Disaster recovery

- **Backup location and restore procedure**:
- **RTO / RPO targets**: <!-- if defined -->
