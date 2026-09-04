class ClaudeSessions < Formula
  include Language::Python::Shebang

  desc "List, search, resume, watch, and manage Claude Code sessions"
  homepage "https://github.com/cnilesh136/homebrew-tools"
  url "https://github.com/cnilesh136/homebrew-tools/archive/refs/tags/claude-sessions-v0.4.0.tar.gz"
  sha256 "5e6caadaaf2ca5412b716e20f9f18d225f44d5f73ecaae2c59f332b4da5b8959"
  license "MIT"

  depends_on "python@3.13"
  depends_on "terminal-notifier" if OS.mac?

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
    log_path var/"log/claude-sessions-watch.log"
    error_log_path var/"log/claude-sessions-watch.log"
  end

  def caveats
    <<~EOS
      To get a desktop notification whenever a running Claude session
      finishes its turn (`cs watch`), start the background service once:

        brew services start claude-sessions

      It stays enabled across reboots. Logs: #{var}/log/claude-sessions-watch.log
    EOS
  end

  test do
    assert_match "No sessions found", shell_output("#{bin}/claude-sessions list")
  end
end
