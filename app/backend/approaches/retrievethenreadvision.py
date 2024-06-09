from typing import Any, Awaitable, Callable, Optional

from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import ContainerClient
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionMessageParam,
)
from openai_messages_token_helper import build_messages, get_token_limit

from approaches.approach import Approach, ThoughtStep
from core.authentication import AuthenticationHelper
from core.imageshelper import fetch_image


class RetrieveThenReadVisionApproach(Approach):
    """
    Simple retrieve-then-read implementation, using the AI Search and OpenAI APIs directly. It first retrieves
    top documents including images from search, then constructs a prompt with them, and then uses OpenAI to generate an completion
    (answer) with that prompt.
    """

    system_chat_template_gpt4v = (
        "You are an intelligent mentor helping students to have more grasp on their course content., The documents contain text, graphs, tables and images. "
        + "Each image source has the file name in the top left corner of the image with coordinates (10,10) pixels and is in the format SourceFileName:<file_name> "
        + "Each text source starts in a new line and has the file name followed by colon and the actual information "
        + "Always include the source name from the image or text for each fact you use in the response in the format: [filename] "
        + "Find the learning objective from asked question, and shape your answer w.r.t learning objectives. Try to mention learning objective explicitly in your answer."
        + "If you are asked for generate multiple choice quiz questions or reflective text based questions, generate questions based on the sources."
        + "You must mention the correct answer in the below and explain properly why it's correct."
        + "Answer the following question using only the data provided in the sources below. "
        + "For tabular information return it as an html table. Do not return markdown format. "
        + "The text and image source can be the same file name, don't use the image title when citing the image source, only use the file name as mentioned "
        + "If you cannot answer using the sources below, say you don't know. Return just the answer without any input texts "
    )

    def __init__(
        self,
        *,
        search_client: SearchClient,
        blob_container_client: ContainerClient,
        openai_client: AsyncOpenAI,
        auth_helper: AuthenticationHelper,
        gpt4v_deployment: Optional[str],
        gpt4v_model: str,
        embedding_deployment: Optional[str],  # Not needed for non-Azure OpenAI or for retrieval_mode="text"
        embedding_model: str,
        embedding_dimensions: int,
        sourcepage_field: str,
        content_field: str,
        query_language: str,
        query_speller: str,
        vision_endpoint: str,
        vision_token_provider: Callable[[], Awaitable[str]]
    ):
        self.search_client = search_client
        self.blob_container_client = blob_container_client
        self.openai_client = openai_client
        self.auth_helper = auth_helper
        self.embedding_model = embedding_model
        self.embedding_deployment = embedding_deployment
        self.embedding_dimensions = embedding_dimensions
        self.sourcepage_field = sourcepage_field
        self.content_field = content_field
        self.gpt4v_deployment = gpt4v_deployment
        self.gpt4v_model = gpt4v_model
        self.query_language = query_language
        self.query_speller = query_speller
        self.vision_endpoint = vision_endpoint
        self.vision_token_provider = vision_token_provider
        self.gpt4v_token_limit = get_token_limit(gpt4v_model)

    async def run(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> dict[str, Any]:
        q = messages[-1]["content"]
        if not isinstance(q, str):
            raise ValueError("The most recent message content must be a string.")

        overrides = context.get("overrides", {})
        auth_claims = context.get("auth_claims", {})
        has_text = overrides.get("retrieval_mode") in ["text", "hybrid", None]
        has_vector = overrides.get("retrieval_mode") in ["vectors", "hybrid", None]
        vector_fields = overrides.get("vector_fields", ["embedding"])

        include_gtpV_text = overrides.get("gpt4v_input") in ["textAndImages", "texts", None]
        include_gtpV_images = overrides.get("gpt4v_input") in ["textAndImages", "images", None]

        use_semantic_captions = True if overrides.get("semantic_captions") and has_text else False
        top = overrides.get("top", 3)
        minimum_search_score = overrides.get("minimum_search_score", 0.0)
        minimum_reranker_score = overrides.get("minimum_reranker_score", 0.0)
        filter = self.build_filter(overrides, auth_claims)
        use_semantic_ranker = overrides.get("semantic_ranker") and has_text

        # If retrieval mode includes vectors, compute an embedding for the query

        vectors = []
        if has_vector:
            for field in vector_fields:
                vector = (
                    await self.compute_text_embedding(q)
                    if field == "embedding"
                    else await self.compute_image_embedding(q)
                )
                vectors.append(vector)

        # Only keep the text query if the retrieval mode uses text, otherwise drop it
        query_text = q if has_text else None

        results = await self.search(
            top,
            query_text,
            filter,
            vectors,
            use_semantic_ranker,
            use_semantic_captions,
            minimum_search_score,
            minimum_reranker_score,
        )

        image_list: list[ChatCompletionContentPartImageParam] = []
        user_content: list[ChatCompletionContentPartParam] = [{"text": q, "type": "text"}]

        # Process results
        sources_content = self.get_sources_content(results, use_semantic_captions, use_image_citation=True)

        if include_gtpV_text:
            content = "\n".join(sources_content)
            user_content.append({"text": content, "type": "text"})
        if include_gtpV_images:
            for result in results:
                url = await fetch_image(self.blob_container_client, result)
                if url:
                    image_list.append({"image_url": url, "type": "image_url"})
            user_content.extend(image_list)

        response_token_limit = 1024
        updated_messages = build_messages(
            model=self.gpt4v_model,
            system_prompt=overrides.get("prompt_template", self.system_chat_template_gpt4v),
            new_user_content=user_content,
            max_tokens=self.gpt4v_token_limit - response_token_limit,
        )
        chat_completion = (
            await self.openai_client.chat.completions.create(
                model=self.gpt4v_deployment if self.gpt4v_deployment else self.gpt4v_model,
                messages=updated_messages,
                temperature=overrides.get("temperature", 0.3),
                max_tokens=response_token_limit,
                n=1,
            )
        ).model_dump()

        data_points = {
            "text": sources_content,
            "images": [d["image_url"] for d in image_list],
        }

        extra_info = {
            "data_points": data_points,
            "thoughts": [
                ThoughtStep(
                    "Search using user query",
                    query_text,
                    {
                        "use_semantic_captions": use_semantic_captions,
                        "use_semantic_ranker": use_semantic_ranker,
                        "top": top,
                        "filter": filter,
                        "vector_fields": vector_fields,
                    },
                ),
                ThoughtStep(
                    "Search results",
                    [result.serialize_for_results() for result in results],
                ),
                ThoughtStep(
                    "Prompt to generate answer",
                    [str(message) for message in updated_messages],
                    (
                        {"model": self.gpt4v_model, "deployment": self.gpt4v_deployment}
                        if self.gpt4v_deployment
                        else {"model": self.gpt4v_model}
                    ),
                ),
            ],
        }

        completion = {}
        completion["message"] = chat_completion["choices"][0]["message"]
        completion["context"] = extra_info
        completion["session_state"] = session_state
        return completion
