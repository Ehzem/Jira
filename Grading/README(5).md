# Jira Usage Dashboard v2

A read-only Streamlit dashboard for analysing Jira issue creation activity.

## Current features

- Connect to any Jira Cloud site the account can browse
- Choose start and end dates from calendar controls
- Group issue creation by:
  - Daily
  - Weekly
  - Biweekly
- Filter by one or more Jira projects
- Filter by issue type
- Analyse one user, several users, or all creators
- Compare multiple creators on the same chart
- See:
  - total issues created
  - average per period
  - active periods
  - peak period
  - per-user creation totals and percentage share
- View the exact issues each user created
- Keep Creator, Reporter, and Assignee separate

## User fields

**Creator** is used for "who created what" analytics.

- Creator = account that actually created the Jira issue
- Reporter = person recorded as reporting/requesting the issue
- Assignee = current owner of the work

This distinction makes the dashboard work for both single-user and multi-user
Jira sites without confusing issue creation with work assignment.

## Install

```powershell
pip install -r requirements.txt
```

## Run

```powershell
streamlit run jira_usage_dashboard.py
```

## Credentials

Enter:

- Jira Cloud site URL
- Atlassian account email
- API token

The dashboard only reads Jira data. It does not update Jira.


## Added in v3

A second creator visualization now ranks users by **total issues created per person**
for the selected date range.

The dashboard now provides two complementary creator views:

1. **Creation over time** — shows when each person creates issues, grouped daily, weekly, or biweekly.
2. **Total issues created per person** — shows who created the most issues overall in the selected period.

The total-per-person chart is horizontal so user names stay readable on multi-user Jira sites.
