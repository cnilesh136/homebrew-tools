class ClaudeSessions < Formula
  include Language::Python::Shebang

  desc "List, resume, delete, and start Claude Code sessions from one picker"
  homepage "https://github.com/cnilesh136/homebrew-tools"
  url "https://github.com/cnilesh136/homebrew-tools/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "a930a0cefe757682cc2270c361eb9b6b2217af243fc561134a23b2fbe21917ee"
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
