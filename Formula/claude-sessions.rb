class ClaudeSessions < Formula
  include Language::Python::Shebang

  desc "List, resume, delete, and start Claude Code sessions from one picker"
  homepage "https://github.com/cnilesh136/homebrew-tools"
  url "https://github.com/cnilesh136/homebrew-tools/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "be722fb1fe721602e8e7093facd711cfe5cd3f2b01537288226504c6c27cbdd4"
  license "MIT"

  depends_on "python@3.13"

  def install
    rewrite_shebang detected_python_shebang, "claude_sessions.py"
    bin.install "claude_sessions.py" => "claude-sessions"
    bin.install_symlink "claude-sessions" => "cs"
  end

  test do
    assert_match "No sessions found", shell_output("#{bin}/claude-sessions list")
  end
end
