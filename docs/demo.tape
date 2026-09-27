# Terminal demo: mirror a Figma fixture into git markdown and grep it.
#
# Regenerate from the repo root (writes docs/demo.gif):
#   uv sync
#   vhs docs/demo.tape
#
# Requires VHS (https://github.com/charmbracelet/vhs) plus ttyd, ffmpeg,
# and ripgrep. `uv sync` provides the `figmaclaw` console script the tape
# uses. The fixture client in docs/demo_fixture.py needs no Figma token.

Output docs/demo.gif

Set Shell "bash"
Set FontFamily "Inter"
Set FontSize 16
Set Width 1400
Set Height 980
Set Padding 20
Set TypingSpeed 30ms
Env PS1 "$ "

Hide
Type@1ms `export PATH="$PATH:$HOME/.local/bin:/exec-daemon"`
Enter
Type@1ms `ROOT=$(pwd)`
Enter
Type@1ms `source "$ROOT/.venv/bin/activate"`
Enter
Type@1ms `export PS1='$ '`
Enter
Type@1ms `rm -rf /tmp/demo && mkdir -p /tmp/demo && cd /tmp/demo && git init -q && git config user.email demo@example.com && git config user.name demo`
Enter
Type@1ms "clear"
Enter
Show

Type "figmaclaw --version"
Enter
Wait+Screen /figmaclaw [0-9]/
Sleep 800ms

Type "figmaclaw init"
Enter
Wait+Screen /Next steps/
Sleep 1200ms

Type `python $ROOT/docs/demo_fixture.py`
Enter
Wait+Screen /Wrote/
Sleep 1200ms

Type "rg -n Checkout figma"
Enter
Wait+Screen /no description yet/
Sleep 1500ms

Type "figmaclaw inspect figma/shop-demo01/pages/checkout-12-40.md"
Enter
Wait+Screen /21:3/
Sleep 1500ms

Type "git add figma .figma-sync && git status --short"
Enter
Wait+Screen /checkout-12-40.md/
Sleep 2s
