Output assets/demo.gif

Require echo

Set Shell "zsh"
Set FontSize 13
Set Width 920
Set Height 580
Set Theme "Catppuccin Mocha"
Set Padding 24
Set TypingSpeed 60ms
Set PlaybackSpeed 4.0

Hide
Type "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH && clear && rm -rf ~/.claude/teams/review ~/.claude/tasks/review"
Enter
Sleep 800ms
Show

Type "cd /tmp/demo_project && claude --dangerously-skip-permissions"
Sleep 600ms
Enter
Sleep 8s

# Accept the bypass permissions warning dialog (select "Yes, I accept")
Down
Enter
Sleep 8s

Type "/advisor ."
Enter
Sleep 360s
