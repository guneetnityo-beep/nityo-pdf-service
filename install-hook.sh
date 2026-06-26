#!/bin/bash
HOOK=.git/hooks/pre-push
cat > $HOOK << 'HOOK'
#!/bin/bash
RESULT=$(git show HEAD:template.html 2>/dev/null | grep "smEmail" | head -1)
if [ -z "$RESULT" ]; then
  echo "ERROR: Wrong template committed. Push blocked."
  echo "Fix: git checkout c279802 -- template.html && git commit -m 'restore' && git push"
  exit 1
fi
echo "Template check passed."
exit 0
HOOK
chmod +x $HOOK
echo "Hook installed - bad template pushes now blocked"
