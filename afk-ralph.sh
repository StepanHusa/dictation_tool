#!/bin/bash
set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <iterations>"
  echo "Example: $0 20"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for ((i=1; i<=$1; i++)); do
  echo "=== Iteration $i / $1 ==="

  result=$(claude --permission-mode acceptEdits -p "@PRD.md @progress.txt
1. Read the PRD and progress file carefully.
2. Find the highest-priority incomplete task (first unchecked [ ] item) and implement it fully.
3. Run \`python3 -m py_compile dictate.py\` to check for syntax errors if dictate.py was modified.
4. Mark the task as done ([x]) in PRD.md.
5. Append a one-line summary of what you did to progress.txt.
6. Stage and commit your changes with a descriptive message.
ONLY WORK ON A SINGLE TASK PER ITERATION.
If all tasks in PRD.md are complete and all success criteria are met, output <promise>COMPLETE</promise>." \
    2>&1)

  echo "$result"

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then
    echo ""
    echo "=== PRD complete after $i iterations. ==="
    exit 0
  fi
done

echo ""
echo "=== Reached $1 iterations without completion. Check PRD.md and progress.txt. ==="
