# Terminal demo: mirror a Figma page into git as markdown, then grep it.
#
# Offline: docs/demo_fixture.py serves a bundled Checkout page to figmaclaw's
# real pull code (pull_file) in place of the Figma REST API, so no token or
# network is needed. With a real file, `figmaclaw track <file-key>` does this step.
#
# Regenerate docs/demo.gif from the repo root on Linux or macOS:
#   uv sync
#   vhs docs/demo.tape
#
# Needs VHS (https://github.com/charmbracelet/vhs) with ttyd and ffmpeg,
# plus ripgrep (`rg`) and the DejaVu Sans Mono font.

Output docs/demo.gif

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 15
Set Width 1200
Set Height 900
Set Padding 24
Set TypingSpeed 35ms
Env PS1 "$ "

Hide
Type@1ms `ROOT=$(pwd) && source "$ROOT/.venv/bin/activate" && export PS1='$ '`
Enter
Type@1ms `rm -rf /tmp/figmaclaw-demo && mkdir -p /tmp/figmaclaw-demo && cd /tmp/figmaclaw-demo && git init -q`
Enter
Type@1ms "clear"
Enter
Show

Type "figmaclaw init   # scaffold the GitHub Actions that keep figma/ in sync"
Enter
Wait+Screen /Next steps/
Sleep 1500ms

Type "python $ROOT/docs/demo_fixture.py   # offline stand-in for: figmaclaw track <file-key>"
Enter
Wait+Screen /checkout-12-40\.md/
Sleep 2s

Type "clear"
Enter
Type "cat figma/shop-demo01/pages/checkout-12-40.md"
Enter
Wait+Screen /Payment declined"\]\s*--> n21_2/
Sleep 4s

Type "clear"
Enter
Type "rg -n 'Payment declined' figma   # agents read design context from files"
Enter
Wait+Screen /n22_1/
Sleep 2s

Type "git add figma .figma-sync .github && git status --short"
Enter
Wait+Screen /figmaclaw-webhook\.yaml/
Sleep 3s
