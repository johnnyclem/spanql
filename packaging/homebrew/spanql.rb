# Homebrew formula for spanql (for the johnnyclem/homebrew-spanql tap).
#
# Resources are generated from uv.lock so the bottle pins exactly what CI tested:
#   uv export --no-dev --extra pdf --no-hashes --no-emit-project \
#     | python3 packaging/homebrew/gen_resources.py
#
# The tree-sitter `code` extra is left out: Homebrew builds resources from sdists, and the
# tree-sitter-typescript 0.23.2 sdist doesn't compile (missing tree_sitter/parser.h for tsx).
# Python symbols use the stdlib ast; TS/JS/Go symbols fall back to a regex extractor.
#
# The repository and has no release tag yet, so this formula is HEAD-only.
# On the first release add:
#   url "https://github.com/johnnyclem/spanql/archive/refs/tags/v0.1.0.tar.gz"
#   sha256 "<sha256 of that tarball>"
class Spanql < Formula
  include Language::Python::Virtualenv

  desc "Grounded Q&A over docs, logs, files and code with traceable formulas"
  homepage "https://github.com/johnnyclem/spanql"
  license :cannot_represent # TBD: John to choose a license before release
  head "https://github.com/johnnyclem/spanql.git", branch: "main"

  depends_on "llama.cpp" # provides llama-server for model.runtime = "managed"
  depends_on "python@3.12"

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/a1/98/7e457a3b014976887a050561955c0b76e1e7e2d6e27b78ecef2ef80518f2/anyio-4.16.0.tar.gz"
    sha256 "59bb03251907983d1603d27c3087ddb1e93299ea2163adcdba37899a4fb12ffb"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/fd/11/b524cbd9bbdaca58608e4cf4053fd463417549e1959fa8d3aa18319f9555/certifi-2026.10.23.tar.gz"
    sha256 "a9895e6e2758b371e0545e321a3a8fd1fcfd9829ada8824a936dfb16aaaf36cd"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/45/37/edbf1b0707709013c0cf2e8317e03d97ed29bf4cac709fe328904ae92a38/click-8.5.1.tar.gz"
    sha256 "9bbfada76703fd1d784b83eea012e3e8c6a001a876543ef104fa56aaaeb072b4"
  end

  resource "h11" do
    url "https://files.pythonhosted.org/packages/01/ee/02a2c011bdab74c6fb3c75474d40b3052059d95df7e73351460c8588d963/h11-0.16.0.tar.gz"
    sha256 "4e35b956cf45792e4caa5885e69fba00bdbc6ffafbfa020300e549b208ee5ff1"
  end

  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/06/94/82699a10bca87a5556c9c59b5963f2d039dbd239f25bc2a63907a05a14cb/httpcore-1.0.9.tar.gz"
    sha256 "6e34463af53fd2ab5d807f399a9b45ea31c3dfa2276f15a2c3f00afff6e176e8"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/b1/df/48c586a5fe32a0f01324ee087459e112ebb7224f646c0b5023f5e79e9956/httpx-0.28.1.tar.gz"
    sha256 "75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/f5/08/8eea9d4b8302028f3abb2c0813953f7aec26d33b7a8960ed760e65ff29fa/idna-3.20.tar.gz"
    sha256 "a7db850025b95ded1eae8a46181a1a6c56c92c96f0e2b005d9ff8dc0210cab44"
  end

  resource "pathspec" do
    url "https://files.pythonhosted.org/packages/5a/82/42f767fc1c1143d6fd36efb827202a2d997a375e160a71eb2888a925aac1/pathspec-1.1.1.tar.gz"
    sha256 "17db5ecd524104a120e173814c90367a96a98d07c45b2e10c2f3919fff91bf5a"
  end

  resource "pypdf" do
    url "https://files.pythonhosted.org/packages/96/5f/f41984a8150ec37c6ee50fa6fa1868d065337b65ba510a155848b2ce0b62/pypdf-7.1.1.tar.gz"
    sha256 "2eff4e2fc5fb22a6d6711e6f1166cdc9ab5765d90cd0d77dfea3f4aa91a27b1d"
  end

  resource "starlette" do
    url "https://files.pythonhosted.org/packages/48/bb/3d8757f21e9a53d9035c9eb039a777047139a16bfdbedfc7a58241c3d914/starlette-1.10.0.tar.gz"
    sha256 "b8399098d4c1e3522734f7235dc79b1fc967f96cf139c81e0fa30479e0685f9b"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/da/34/30e9280707135d2cfc589dfff3cb796bd07a3aeb1a3e415ba09dd89d7bb4/uvicorn-0.54.0.tar.gz"
    sha256 "a2e33cbfaa0306f8e6b0c13e0cb89d7d7a2da3e62b90c66e18c33d9807b28620"
  end

  def install
    virtualenv_install_with_resources
  end

  post_install_steps do
    mkdir_p "spanql/models", base: :var
    mkdir_p "log", base: :var
    # writes $(brew --prefix)/etc/spanql/config.toml only if it doesn't exist yet
    run "spanql", base: :opt_bin, args: ["init", "--homebrew"], writable_paths: ["spanql"], writable_base: :etc
  end

  def caveats
    <<~EOS
      Config: #{etc}/spanql/config.toml (add [[sources]] for the folders to query)
      Download the default model (2.7 GB) once:
        spanql model pull qwen3.5-4b-instruct-q4_k_m
      Then start the service (UI at http://127.0.0.1:7717):
        brew services start spanql
      macOS may ask for permission the first time the service reads ~/Documents,
      ~/Desktop or ~/Downloads.
    EOS
  end

  service do
    run [opt_bin/"spanql", "serve", "--config", etc/"spanql/config.toml"]
    keep_alive true
    working_dir var/"spanql"
    log_path var/"log/spanql.log"
    error_log_path var/"log/spanql.err.log"
    environment_variables SPANQL_MODELS: var/"spanql/models", PATH: std_service_path_env
  end

  test do
    assert_match "selftest passed", shell_output("#{bin}/spanql selftest --no-model")
    assert_match version.to_s, shell_output("#{bin}/spanql --version") if build.stable?
  end
end
