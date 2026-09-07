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
  url "https://files.pythonhosted.org/packages/f1/8a/65cb0e23d4c2301c1ba0756d3396c72adb3e580e2e392385f791bde601c7/spielos-10.3.0.tar.gz"
  sha256 "eb6f5e40cc490ba8858cceb898d25f10e376bf28d104e265edd1a6a7e74b7f8c"
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
