# Homebrew formula for SpielOS (spielos)
#
# Installs the `spielos` Python package into a vendored virtualenv and wraps it
# so the `spielos` command invokes `python3 -m company` from the vendored
# interpreter. This keeps the macOS/Linux `spielos` CLI working through the real
# Python runtime rather than a stub.
class Spielos < Formula
  include Language::Python::Virtualenv

  desc "AI company operating system with durable Goals, Departments, and approvals"
  homepage "https://spielos.xyz"
  # Content-addressed source sdist published by PyPI.
  url "https://files.pythonhosted.org/packages/d1/8c/b8b78595236f49f4d57e24a2ed4272c744ab9d6ecc679643a64093a5485e/spielos-10.3.1.tar.gz"
  sha256 "9aebd95573d10c16b3593807776c94bd9fe49f6b8f2d8c279fc4f00fc35b1142"
  license "MIT"
  head "https://github.com/ShayanSpiel/SpielOS.git", branch: "main"

  depends_on "python@3.12"

  def install
    venv = virtualenv_create(libexec, "python@3.12")
    venv.pip_install buildpath

    # Thin wrapper: exec the vendored python running `python3 -m company`.
    (bin/"spielos").write <<~EOS
      #!/bin/bash
      exec "#{libexec}/bin/python3" -m company "$@"
    EOS
    chmod 0755, bin/"spielos"
  end

  test do
    assert_match "spielos", shell_output("#{bin}/spielos --version")
  end
end
