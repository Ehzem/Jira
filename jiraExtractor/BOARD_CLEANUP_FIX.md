# Default Scrum board cleanup in v3.4

Jira's Scrum project template automatically creates a board named `<PROJECT_KEY> board`. The TES source export contains only `Main Board` and `Active Sprint Board`, so keeping the generated board makes the destination differ from the source.

v3.4 removes that generated board only when all of the following are true:

1. The board is located in the destination project.
2. Its name exactly matches `<TARGET_PROJECT_KEY> board` (case-insensitive).
3. The source export does not contain a board with that name.

No other destination board is automatically deleted. The importer records the cleanup in `board_cleanup_verification.json`.
