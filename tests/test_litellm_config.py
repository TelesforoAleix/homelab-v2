from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_model_config_has_only_the_fixed_purposes_and_no_fallback():
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
    rows = {row["model_name"]: row["litellm_params"] for row in config["model_list"]}
    assert set(rows) == {
        "chat",
        "chat:high",
        "chat:xhigh",
        "embed",
        "embed-large",
        "embed-large-bulk",
        "grade",
        "translate",
        "vision",
        "vision:xhigh",
        "image-nano-banana-2.1",
        "image-gpt-2.5-flare",
        "image-gpt-2.5-sunburst",
    }
    for purpose, effort in [
        ("chat", "low"),
        ("chat:high", "medium"),
        ("chat:xhigh", "high"),
        ("grade", "low"),
        ("translate", "low"),
    ]:
        assert rows[purpose] == {
            "model": "openai/deepseek/deepseek-v4.1-flash",
            "api_base": "https://ai-gateway.vercel.sh/v1",
            "api_key": "os.environ/HOMELAB_GATEWAY_API_KEY",
            "reasoning_effort": effort,
        }
    assert rows["embed"] == {
        "model": "openai/llama-nemotron-embed-1b-v2",
        "api_base": "http://llama-embed:8080/v1",
        "api_key": "none",
    }
    assert rows["embed-large"] == {
        "model": "openai/Qwen3-Embedding-4B",
        "api_base": "http://llama-embed-large:8080/v1",
        "api_key": "none",
    }
    assert rows["embed-large-bulk"] == {
        "model": "openai/Qwen3-Embedding-4B",
        "api_base": "os.environ/HOMELAB_MAC_EMBED_URL",
        "api_key": "none",
        "timeout": 300.0,
    }
    info = {row["model_name"]: row.get("model_info", {}) for row in config["model_list"]}
    assert info["embed"] == {
        "mode": "embedding",
        "input_prefixes": {"query": "query: ", "passage": "passage: "},
    }
    assert info["embed-large"] == {
        "mode": "embedding",
        "input_prefixes": {
            "query": (
                "Instruct: Given a question, retrieve passages that answer the question\nQuery:"
            ),
            "passage": "",
        },
    }
    assert info["embed-large-bulk"] == {**info["embed-large"], "connect_timeout": 3.0}
    assert rows["vision"] == {
        "model": "openai/vision",
        "api_base": "os.environ/HOMELAB_MAC_VISION_URL",
        "api_key": "none",
        "timeout": 300.0,
    }
    assert (
        next(row for row in config["model_list"] if row["model_name"] == "vision")["model_info"][
            "connect_timeout"
        ]
        == 3.0
    )
    assert rows["vision:xhigh"] == {
        "model": "openai/deepseek/deepseek-v4.1-flash",
        "api_base": "https://ai-gateway.vercel.sh/v1",
        "api_key": "os.environ/HOMELAB_GATEWAY_API_KEY",
    }
    router = config["router_settings"]
    assert router["num_retries"] == 0
    assert all(
        router[name] == []
        for name in ["fallbacks", "context_window_fallbacks", "content_policy_fallbacks"]
    )
    assert "default_model" not in config and "master_key" not in config["general_settings"]
    assert not (ROOT / "config/routes.yaml").exists()


def test_proxy_is_the_only_network_member_and_gateway_secret_holder():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]
    proxy = services["litellm"]
    assert proxy["image"].startswith("ghcr.io/berriai/litellm:v1.103.3@sha256:")
    assert len(proxy["image"].rsplit(":", 1)[-1]) == 64
    assert proxy["user"] == "10001:10001"
    assert proxy["cap_drop"] == ["ALL"]
    assert proxy["security_opt"] == ["no-new-privileges:true"]
    assert proxy["restart"] == "unless-stopped"
    assert proxy["logging"] == {"driver": "journald"}
    assert "ports" not in proxy
    assert proxy["networks"] == ["default", "models"]
    assert compose["networks"]["models"] == {"name": "homelab-models"}
    assert proxy["secrets"] == ["gateway_api_key", "image_gateway_api_key"]
    assert compose["secrets"]["image_gateway_api_key"] == {
        "file": "./secrets/image_gateway_api_key"
    }
    assert "HOMELAB_GATEWAY_API_KEY" not in proxy["environment"]
    assert "HOMELAB_IMAGE_GATEWAY_API_KEY" not in proxy["environment"]
    assert proxy["environment"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert proxy["environment"]["DISABLE_ADMIN_UI"] == "True"
    for name, service in services.items():
        if name != "litellm":
            assert "models" not in service.get("networks", [])
            assert "gateway_api_key" not in service.get("secrets", [])
            assert "image_gateway_api_key" not in service.get("secrets", [])
    for name in ["api", "worker"]:
        assert not any(
            "GATEWAY" in key or "LOCAL_BASE" in key for key in services[name]["environment"]
        )
        assert services[name]["depends_on"]["litellm"] == {"condition": "service_healthy"}


def test_image_profiles_and_key_isolation():
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text())
    expected = {
        "image-nano-banana-2.1": (
            "google/gemini-nano-banana-2.1",
            14,
            "chat/completions",
            "gemini",
        ),
        "image-gpt-2.5-flare": ("openai/gpt-image-2.5-flare", 16, "images/edits", "openai"),
        "image-gpt-2.5-sunburst": ("openai/gpt-image-2.5-sunburst", 16, "images/edits", "openai"),
    }
    for row in config["model_list"]:
        spec = row["litellm_params"]
        if row["model_name"] not in expected:
            assert spec["api_key"] != "os.environ/HOMELAB_IMAGE_GATEWAY_API_KEY"
            continue
        model, references, endpoint, family = expected[row["model_name"]]
        assert ":" not in row["model_name"]
        assert spec["model"] == "openai/" + model
        assert spec["api_base"] == "https://ai-gateway.vercel.sh/v1"
        assert spec["api_key"] == "os.environ/HOMELAB_IMAGE_GATEWAY_API_KEY"
        assert spec["timeout"] >= 300
        info = row["model_info"]
        assert info["mode"] == "image_generation"
        assert info["paid"] is True and info["placeholder"] is False
        assert info["endpoint"] == endpoint and info["prompt_family"] == family
        assert info["max_references"] == references and info["reference_max_side"] == 2048
        assert info["display_name"] and info["licence"]
        assert info["seconds_per_image"] > 0
        assert info["response"] in {"b64_json", "url", "message_images"}
        if "sizes" in info:
            assert not {"aspect_ratios", "resolutions", "qualities"} & info.keys()
            assert info["sizes"]
            for size in info["sizes"]:
                assert set(size) == {"value", "aspect_ratio", "resolution", "price"}
                assert size["value"] and size["aspect_ratio"] and size["resolution"]
                assert size["price"] > 0
        else:
            assert info["aspect_ratios"]["field"] and info["aspect_ratios"]["values"]
            assert ("resolutions" in info) != ("qualities" in info)
            setting = info.get("resolutions", info.get("qualities"))
            assert setting["field"] and setting["values"]
            assert setting["field"] != info["aspect_ratios"]["field"]
        for option in info["advanced"]:
            assert {"name", "type", "default"} <= option.keys()
            assert "values" in option or "range" in option
            assert option["name"] != "seed"
        assert all(price > 0 for price in info["price_per_image"].values())
        assert info["price_checked"] == "2026-10-14"


def test_private_mac_url_only_reaches_proxy_and_has_safe_unset_default():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert compose["services"]["litellm"]["environment"]["HOMELAB_MAC_VISION_URL"] == (
        "${HOMELAB_MAC_VISION_URL:-http://127.0.0.1:9/v1}"
    )
    assert "HOMELAB_MAC_VISION_URL=\n" in (ROOT / ".env.example").read_text()
    for name, service in compose["services"].items():
        if name != "litellm":
            assert "HOMELAB_MAC_VISION_URL" not in service.get("environment", {})
    assert compose["services"]["litellm"]["environment"]["HOMELAB_MAC_EMBED_URL"] == (
        "${HOMELAB_MAC_EMBED_URL:-http://127.0.0.1:9/v1}"
    )
    assert "HOMELAB_MAC_EMBED_URL=\n" in (ROOT / ".env.example").read_text()
    for name, service in compose["services"].items():
        if name != "litellm":
            assert "HOMELAB_MAC_EMBED_URL" not in service.get("environment", {})
    import re

    for path in [
        ROOT / "config/litellm.yaml",
        ROOT / "compose.yaml",
        ROOT / ".env.example",
        ROOT / "mac/homelab-vision",
        ROOT / "mac/homelab-embed",
        ROOT / "DECISIONS.md",
        ROOT / "README.md",
        ROOT / "docs/operations.md",
        ROOT / "ARCHITECTURE.md",
    ]:
        assert not re.search(r"\b100\.\d+\.\d+\.\d+\b|[\w-]+\.ts\.net\b", path.read_text())


def test_large_embeddings_are_private_cpu_only_and_hardened():
    import shlex

    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    large = services["llama-embed-large"]
    assert large["image"] == services["llama-embed"]["image"]
    assert large["user"] == "10001:10001"
    assert large["cap_drop"] == ["ALL"]
    assert large["security_opt"] == ["no-new-privileges:true"]
    assert large["logging"] == {"driver": "journald"}
    assert large["restart"] == "unless-stopped"
    assert large["networks"] == ["default"] and "ports" not in large
    assert large["environment"]["LLAMA_CACHE"] == "/models"
    assert large["volumes"] == ["${HOMELAB_MODELS_PATH:-/srv/homelab/models}/embed-large:/models"]
    command = shlex.split(large["command"])
    for flag, value in [
        ("--pooling", "last"),
        ("--embd-normalize", "2"),
        ("-t", "4"),
        ("-ngl", "0"),
    ]:
        assert command[command.index(flag) + 1] == value
    assert "--embeddings" in command
    assert command[command.index("-mu") + 1] == (
        "https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/"
        "f4602530db1d980e16da9d7d3a70294cf5c190be/Qwen3-Embedding-4B-Q8_0.gguf"
    )
    assert services["litellm"]["depends_on"]["llama-embed-large"] == {
        "condition": "service_started"
    }
