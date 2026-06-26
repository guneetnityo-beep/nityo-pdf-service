#!/bin/bash
# Run before every git push to verify template is correct
RESULT=$(git show HEAD:template.html 2>/dev/null | grep "smEmail" | head -1)
if [ -z "$RESULT" ]; then
  echo "WRONG TEMPLATE COMMITTED - do not push"
  echo "Run: git checkout c279802 -- template.html"
  exit 1
else
  echo "Template OK - smEmail found"
  exit 0
fi
