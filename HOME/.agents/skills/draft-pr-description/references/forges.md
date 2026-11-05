Other forges
============

The GitHub-specific commands in *SKILL.md* assume `gh`. The template locations
and the way to read them differ per forge; the rest of the workflow does not.


GitHub
------

Templates live in the repository root, `docs/`, or `.github/`. GitHub checks
these names, case-insensitively:

 -  `.github/pull_request_template.md`
 -  `.github/PULL_REQUEST_TEMPLATE.md`
 -  `docs/pull_request_template.md`
 -  `docs/PULL_REQUEST_TEMPLATE.md`
 -  `pull_request_template.md`
 -  `PULL_REQUEST_TEMPLATE.md`

A `.github/PULL_REQUEST_TEMPLATE/` directory can hold several templates, one
per kind of change. `gh pr create --template <file>` selects one. When the
project has no template of its own, check the organization's `.github`
repository: `github.com/ORG/.github` can supply a default.

~~~~ bash
# Names only, so you can pick the matching template
gh api repos/OWNER/REPO/contents/.github/PULL_REQUEST_TEMPLATE --jq '.[].name'

# Full file
gh api -H "Accept: application/vnd.github.raw" \
  "repos/OWNER/REPO/contents/.github/pull_request_template.md"

# Labels and open milestones
gh label list --repo OWNER/REPO --limit 200
gh api "repos/OWNER/REPO/milestones?state=open&per_page=100" --jq '.[].title'

# Recent merged PRs, the best evidence for language and tone
gh pr list --repo OWNER/REPO --state merged --limit 10 \
  --json number,title,body,author,mergedAt,labels
~~~~

For a fork, run these against the parent repository.
`gh repo view --json parent` reports it, and the pull request lands there.


GitLab
------

Merge request templates live in `.gitlab/merge_request_templates/` as Markdown
files. A project can set one as the default in its settings; without that, the
description starts blank and the author chooses a template by name.

~~~~ bash
glab api "projects/:id/repository/tree?path=.gitlab/merge_request_templates&per_page=100"
glab api "projects/:id/repository/files/.gitlab%2Fmerge_request_templates%2FDefault.md/raw?ref=main"
~~~~

Without `glab`, the same information comes from the instance's REST API
(`GET /projects/:id/repository/files/:file_path/raw?ref=:ref`) or from a plain
checkout of the default branch.

GitLab labels are flat and often scoped (`bug`, `type::feature`); use them by
their full name, including the `::`. Milestones are project-wide and have start
and due dates. GitLab has no PR draft flag equivalent to GitHub's; mark the
title with `Draft:` when the change is not ready, following the project's own
convention.


Gitea and Forgejo
-----------------

Both read pull request templates from the repository root and from `.gitea/` or
`.forgejo/`:

 -  `.gitea/PULL_REQUEST_TEMPLATE.md`
 -  `.forgejo/PULL_REQUEST_TEMPLATE.md`
 -  `PULL_REQUEST_TEMPLATE.md`

Their APIs follow the Gitea/Forgejo schema, and the web UI accepts labels and
milestones by name. There is no standard CLI as widely installed as `gh`; read
the checkout directly when the instance's API is not reachable.


Bitbucket
---------

Bitbucket Cloud reads `PULL_REQUEST_TEMPLATE.md` from the repository root and
from `.bitbucket/`. It has no label concept equivalent to GitHub's; use the
description's own structure and the project's conventions. Bitbucket Server and
Data Center use a repository-level default description configured in the web
UI, which a checkout cannot reveal. Say that you could not verify the default
rather than guessing it.
