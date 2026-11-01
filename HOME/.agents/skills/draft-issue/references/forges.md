Other forges
============

The GitHub-specific commands in *SKILL.md* assume `gh`. The template locations
and the way to read them differ per forge; the rest of the workflow does not.


GitHub
------

Templates live in `.github/ISSUE_TEMPLATE/`: issue forms as `*.yml`, Markdown
templates as `*.md`, and `config.yml` for the chooser. A legacy single
`ISSUE_TEMPLATE.md` in the root, `docs/`, or `.github/` also counts when the
directory is absent. If the project has no templates of its own, the
organization's `.github` repository can supply defaults, so check
`github.com/ORG/.github` before concluding there is none.

~~~~ bash
# Names only, so you can pick the matching template
gh api repos/OWNER/REPO/contents/.github/ISSUE_TEMPLATE --jq '.[].name'

# Full file
gh api -H "Accept: application/vnd.github.raw" \
  "repos/OWNER/REPO/contents/.github/ISSUE_TEMPLATE/01_bug.yml"

# Issue types, only for repositories owned by an organization
gh api graphql -f query='
  query($owner: String!, $name: String!) {
    repository(owner: $owner, name: $name) {
      issueTypes(first: 50) { nodes { name } }
    }
  }' -F owner=OWNER -F name=REPO \
  --jq '.data.repository.issueTypes.nodes[].name'

gh label list --repo OWNER/REPO --limit 200
gh api "repos/OWNER/REPO/milestones?state=open&per_page=100" --jq '.[].title'
~~~~


GitLab
------

Templates live in `.gitlab/issue_templates/` as Markdown files, and the project
may set one of them as the default. There is no separate description-template
directory; “description template” is just what GitLab calls the contents.

~~~~ bash
glab api "projects/:id/repository/tree?path=.gitlab/issue_templates&per_page=100"
glab api "projects/:id/repository/files/.gitlab%2Fissue_templates%2FDefault.md/raw?ref=main"
~~~~

Without `glab`, the same information comes from the instance's REST API
(`GET /projects/:id/repository/files/:file_path/raw?ref=:ref`) or from a plain
checkout of the default branch.

GitLab labels are flat and often scoped (`bug`, `type::feature`); use them by
their full name, including the `::`. Milestones are project-wide and have start
and due dates. GitLab has no issue types, so omit `type` from the frontmatter.


Gitea and Forgejo
-----------------

Both accept Markdown templates and YAML issue forms. They look in
`.gitea/ISSUE_TEMPLATE/` and `.gitea/issue_template/`, in the repository root
(`ISSUE_TEMPLATE.md`, `ISSUE_TEMPLATE.yaml`), and in `.github/ISSUE_TEMPLATE/`;
Forgejo also reads `.forgejo/ISSUE_TEMPLATE/`. Markdown templates carry their
metadata in a frontmatter block (`name`, `about`, `title`, `labels`,
`assignees`), and YAML forms use the same field types as GitHub's.

~~~~ bash
curl -fsS "https://gitea.example/api/v1/repos/OWNER/REPO/contents/.gitea/ISSUE_TEMPLATE"
curl -fsS "https://gitea.example/api/v1/repos/OWNER/REPO/raw/.gitea/ISSUE_TEMPLATE/01_bug.md"
~~~~

Forgejo and Gitea have no issue types either. Milestones exist per repository,
and issue references are written the same way (`#123`, `owner/repo#123`).


Anything else
-------------

When the tracker is not one of the above (Jira, Linear, a mailing list), look
for `CONTRIBUTING.md`, a `.github/CONTRIBUTING.md`, or a “reporting issues”
section in the README. Those usually state the required fields and the
conventions for the tracker. Follow them and keep the draft in the same
frontmatter shape so the fields stay legible even when nothing can consume them
automatically.
