---
description: ⚡⚡ No research. Only analyze and create an implementation plan
argument-hint: [task]
---

Think.
Activate `planning` skill.

## Your mission
<task>
$ARGUMENTS
</task>

## Workflow
Use `planner` subagent to:
1. Get date: `node -e "console.log(new Date().toISOString().slice(2,10).replace(/-/g,''))"`
2. Create a directory named `plans/YYMMDD-plan-name` (eg. `plans/251101-authentication-and-profile-implementation`).
   Make sure you pass the directory path to every subagent during the process.
3. Follow strictly to the "Plan Creation & Organization" rules of `planning` skill.
4. Analyze the codebase by reading `codebase-summary.md`, `code-standards.md`, `system-architecture.md` and `project-overview-pdr.md` file.
5. Gathers all information and create an implementation plan of this task.
6. Ask user to review the plan.

## Output Requirements

**Plan Directory Structure**
```
plans/
└── YYMMDD-plan-name/
    ├── reports/
    │   ├── XX-report.md
    │   └── ...
    ├── plan.md
    ├── phase-XX-phase-name-here.md
    └── ...
```

**Plan File Specification**
- Save the overview access point at `plans/YYMMDD-plan-name/plan.md`. Keep it generic, under 80 lines, and list each implementation phase with status and progress plus links to phase files.
- For each phase, create `plans/YYMMDD-plan-name/phase-XX-phase-name-here.md` containing the following sections in order: Context links (reference parent plan, dependencies, docs), Overview (date, description, priority, implementation status, review status), Key Insights, Requirements, Architecture, Related code files, Implementation Steps, Todo list, Success Criteria, Risk Assessment, Security Considerations, Next steps.

## Important Notes
- **IMPORTANT:** Ensure token efficiency while maintaining high quality.
- **IMPORTANT:** Analyze the skills catalog and activate the skills that are needed for the task during the process.
- **IMPORTANT:** Sacrifice grammar for the sake of concision when writing reports.
- **IMPORTANT:** In reports, list any unresolved questions at the end, if any.
- **IMPORTANT**: **Do not** start implementing.
