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
  url "https://files.pythonhosted.org/packages/b0/34/ab2b3ea32339cee2d475bb67feb1c4dbf2126d32b0507888d414e8a49bd5/spielos-10.3.2.tar.gz"
  sha256 "fc8ff25e16219fad5a637d1c73279a812c90224519abc10e65c106201c0a3a83"
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
