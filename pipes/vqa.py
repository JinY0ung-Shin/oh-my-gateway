import textwrap
import asyncio
from typing import List
from pydantic import Field, BaseModel
from common import UnifiedModelClient
from common.exceptions import VQAError, ConnectionError, ValidationError


VQA_NONREASONING_SYS_PROMPT = """당신은 이미지 분석 전문가입니다.
사용자가 제공한 이미지를 분석하여 질문에 답변하세요.
답변은 간결하고 명확하게 작성하세요."""


def pil_to_base64(img, use_data_url: bool = True) -> str:
    import io
    import base64

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_str = base64.b64encode(buffer.read()).decode("utf-8")

    if use_data_url:
        return f"data:image/png;base64,{img_str}"
    return img_str


def read_image(image_path_or_url: str):
    from PIL import Image
    import requests
    import io

    if image_path_or_url.startswith("http://") or image_path_or_url.startswith("https://"):
        response = requests.get(image_path_or_url, timeout=30)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content))
    elif image_path_or_url.startswith("data:image"):
        import base64
        header, encoded = image_path_or_url.split(",", 1)
        data = base64.b64decode(encoded)
        return Image.open(io.BytesIO(data))
    else:
        return Image.open(image_path_or_url)


class VQAInput(BaseModel):
    query: str = Field(description="이미지에 대한 질문")
    images: List[str] = Field(
        description="이미지의 경로 또는 base64 인코딩 이미지의 list"
    )

    def to_payload(self):
        try:
            base64imgs = [
                pil_to_base64(read_image(img), use_data_url=True) for img in self.images
            ]
        except Exception:
            base64imgs = self.images

        content = [
            {
                "type": "image_url",
                "image_url": {"url": img},
            }
            for img in base64imgs
        ]
        content.append({"type": "text", "text": self.query})

        return {
            "messages": [
                {"role": "system", "content": VQA_NONREASONING_SYS_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": 2048,
            "temperature": 0,
            "stream": False,
        }


def call_vision_model_sync(
    model_input: str,
    payload: dict
) -> str:
    try:
        client = UnifiedModelClient(verify_ssl=False)

        response = client.query_model(
            model_input=model_input,
            messages=payload["messages"],
            temperature=payload["temperature"],
            max_tokens=payload["max_tokens"],
            stream=False,
            timeout=60
        )

        full_content = ""
        if 'choices' in response and len(response['choices']) > 0:
            full_content = response['choices'][0]['message']['content']
        else:
            raise RuntimeError(f"Unexpected response format: {response}")

        return full_content

    except ConnectionError:
        # Re-raise connection errors
        raise
    except Exception as e:
        # Wrap other exceptions in VQAError
        raise VQAError(
            message=f"Vision model API request failed: {e}",
            metadata={"model_input": model_input, "error": str(e)}
        ) from e


VQA_DESCRIPTION = textwrap.dedent("""
    # **이미지 분석 모델**
    이 도구는 사용자가 이미지를 제공한 경우 이미지를 분석하여 사용자의 질문에 대해 답변합니다.
    이미지 태그(`<img alt="customInput" src="blob:https://chatdram.samsungds.net/...">`)가 포함된 요청에 사용합니다.
""").strip()


async def vqa_search(
    query: str,
    images: List[str],
    vision_model: str = "GaussO2-SAM-VL"
) -> dict:
    try:
        input_data = VQAInput(query=query, images=images)
        payload = input_data.to_payload()

        final_text = await asyncio.to_thread(
            call_vision_model_sync, vision_model, payload
        )

        output_dict = {
            "message": "이미지 분석 완료",
            "payload": {"answer": final_text},
            "type_ui": "VQA",
            "model_used": vision_model
        }

        return output_dict

    except (VQAError, ConnectionError, ValidationError) as e:
        # Return error response for known exceptions
        error_message = str(e)
        if isinstance(e, VQAError):
            error_message = f"VQA 분석 오류: {e.message}"
        elif isinstance(e, ConnectionError):
            error_message = f"연결 오류: {e.message}"
        elif isinstance(e, ValidationError):
            error_message = f"입력 검증 오류: {e.message}"

        return {
            "error": error_message,
            "model_used": vision_model,
            "error_code": getattr(e, 'error_code', 'VQA_ERROR'),
            "metadata": getattr(e, 'metadata', {})
        }
    except Exception as e:
        # Wrap unexpected exceptions in VQAError
        vqa_error = VQAError(
            message=f"Unexpected error in VQA search: {e}",
            metadata={"original_error": str(e), "query": query}
        )
        return {
            "error": f"VQA 분석 오류: {vqa_error.message}",
            "model_used": vision_model,
            "error_code": vqa_error.error_code,
            "metadata": vqa_error.metadata
        }
