class ClaudeSessions < Formula
  include Language::Python::Shebang

  desc "List, search, resume, watch, and manage Claude Code sessions"
  homepage "https://github.com/cnilesh136/homebrew-tools"
  url "https://github.com/cnilesh136/homebrew-tools/archive/refs/tags/claude-sessions-v0.5.2.tar.gz"
  sha256 "eed0a7bfd2446a742968c15dc1ac976e417b776bb8f463cc8378263ec2256b4c"
  license "MIT"

  depends_on "python@3.13"

  def install
    cd "tools/claude-sessions" do
      rewrite_shebang detected_python_shebang, "claude_sessions.py"
      bin.install "claude_sessions.py" => "claude-sessions"
      bin.install_symlink "claude-sessions" => "cs"
    end
  end

  service do
    run [opt_bin/"claude-sessions", "watch"]
    keep_alive true
    environment_variables PATH: std_service_path_env
    log_path var/"log/claude-sessions-watch.log"
    error_log_path var/"log/claude-sessions-watch.log"
  end

  def caveats
    <<~EOS
      ┌──────────────────────────────────────────────────────────────────┐
      │  ◆ claude-sessions — mission control for Claude Code sessions    │
      └──────────────────────────────────────────────────────────────────┘

        cs               interactive picker: browse, resume, live status
        cs search "..."  full-text search every conversation you ever had
        cs stats         what your Claude sessions really cost
        cs export <id>   turn a conversation into shareable Markdown
        cs help          the full tour

      Optional: desktop notifications when a session finishes its turn
      or waits for your input (off by default, survives reboots):

        cs service start      enable
        cs service stop       disable anytime
        cs service status     check

      Logs: #{var}/log/claude-sessions-watch.log
    EOS
  end

  test do
    assert_match "No sessions found", shell_output("#{bin}/claude-sessions list")
  end
end
