# Copyright 2023 The Langfun Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Vertex AI generative models."""

import functools
import os
from typing import Annotated, Any

from google.auth import credentials as credentials_lib
import langfun.core as lf
from langfun.core import modalities as lf_modalities
import pyglove as pg


SUPPORTED_MODELS_AND_SETTINGS = {
    'gemini-1.5-pro-preview-0409': pg.Dict(api='gemini', rpm=5),
    'gemini-1.0-pro': pg.Dict(api='gemini', rpm=300),
    'gemini-1.0-pro-vision': pg.Dict(api='gemini', rpm=100),
    # PaLM APIs.
    'text-bison': pg.Dict(api='palm', rpm=1600),
    'text-bison-32k': pg.Dict(api='palm', rpm=300),
    'text-unicorn': pg.Dict(api='palm', rpm=100),
}


@lf.use_init_args(['model'])
class VertexAI(lf.LanguageModel):
  """Language model served on VertexAI."""

  model: pg.typing.Annotated[
      pg.typing.Enum(
          pg.MISSING_VALUE, list(SUPPORTED_MODELS_AND_SETTINGS.keys())
      ),
      (
          'Vertex AI model name. See '
          'https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models '
          'for details.'
      ),
  ]

  project: Annotated[
      str | None,
      (
          'Vertex AI project ID. Or set from environment variable '
          'VERTEXAI_PROJECT.'
      ),
  ] = None

  location: Annotated[
      str | None,
      (
          'Vertex AI service location. Or set from environment variable '
          'VERTEXAI_LOCATION.'
      ),
  ] = None

  credentials: Annotated[
      credentials_lib.Credentials | None,
      (
          'Credentials to use. If None, the default credentials to the '
          'environment will be used.'
      ),
  ] = None

  multimodal: Annotated[bool, 'Whether this model has multimodal support.'] = (
      False
  )

  def _on_bound(self):
    super()._on_bound()
    self.__dict__.pop('_api_initialized', None)

  @functools.cached_property
  def _api_initialized(self):
    project = self.project or os.environ.get('VERTEXAI_PROJECT', None)
    if not project:
      raise ValueError(
          'Please specify `project` during `__init__` or set environment '
          'variable `VERTEXAI_PROJECT` with your Vertex AI project ID.'
      )

    location = self.location or os.environ.get('VERTEXAI_LOCATION', None)
    if not location:
      raise ValueError(
          'Please specify `location` during `__init__` or set environment '
          'variable `VERTEXAI_LOCATION` with your Vertex AI service location.'
      )

    credentials = self.credentials
    # Placeholder for Google-internal credentials.
    from google.cloud.aiplatform import vertexai  # pylint: disable=g-import-not-at-top
    vertexai.init(project=project, location=location, credentials=credentials)
    return True

  @property
  def model_id(self) -> str:
    """Returns a string to identify the model."""
    return f'VertexAI({self.model})'

  @property
  def resource_id(self) -> str:
    """Returns a string to identify the resource for rate control."""
    return self.model_id

  @property
  def max_concurrency(self) -> int:
    """Returns the maximum number of concurrent requests."""
    return self.rate_to_max_concurrency(
        requests_per_min=SUPPORTED_MODELS_AND_SETTINGS[self.model].rpm,
        tokens_per_min=0,
    )

  def _generation_config(
      self, options: lf.LMSamplingOptions
  ) -> Any:  # generative_models.GenerationConfig
    """Creates generation config from langfun sampling options."""
    from google.cloud.aiplatform.vertexai.preview import generative_models  # pylint: disable=g-import-not-at-top
    return generative_models.GenerationConfig(
        temperature=options.temperature,
        top_p=options.top_p,
        top_k=options.top_k,
        max_output_tokens=options.max_tokens,
        stop_sequences=options.stop,
    )

  def _content_from_message(
      self, prompt: lf.Message
  ) -> list[str | Any]:
    """Gets generation input from langfun message."""
    from google.cloud.aiplatform.vertexai.preview import generative_models  # pylint: disable=g-import-not-at-top
    chunks = []
    for lf_chunk in prompt.chunk():
      if isinstance(lf_chunk, str):
        chunk = lf_chunk
      elif self.multimodal and isinstance(lf_chunk, lf_modalities.Image):
        chunk = generative_models.Image.from_bytes(lf_chunk.to_bytes())
      else:
        raise ValueError(f'Unsupported modality: {lf_chunk!r}')
      chunks.append(chunk)
    return chunks

  def _generation_response_to_message(
      self,
      response: Any,  # generative_models.GenerationResponse
  ) -> lf.Message:
    """Parses generative response into message."""
    return lf.AIMessage(response.text)

  def _sample(self, prompts: list[lf.Message]) -> list[lf.LMSamplingResult]:
    assert self._api_initialized, 'Vertex AI API is not initialized.'
    return lf.concurrent_execute(
        self._sample_single,
        prompts,
        executor=self.resource_id,
        max_workers=self.max_concurrency,
        # NOTE(daiyip): Vertex has its own policy on handling
        # with rate limit, so we do not retry on errors.
        retry_on_errors=None,
    )

  def _sample_single(self, prompt: lf.Message) -> lf.LMSamplingResult:
    if self.sampling_options.n > 1:
      raise ValueError(
          f'`n` greater than 1 is not supported: {self.sampling_options.n}.'
      )
    api = SUPPORTED_MODELS_AND_SETTINGS[self.model].api
    match api:
      case 'gemini':
        return self._sample_generative_model(prompt)
      case 'palm':
        return self._sample_text_generation_model(prompt)
      case _:
        raise ValueError(f'Unsupported API: {api}')

  def _sample_generative_model(self, prompt: lf.Message) -> lf.LMSamplingResult:
    """Samples a generative model."""
    model = _VERTEXAI_MODEL_HUB.get_generative_model(self.model)
    input_content = self._content_from_message(prompt)
    response = model.generate_content(
        input_content,
        generation_config=self._generation_config(self.sampling_options),
    )
    usage_metadata = response.usage_metadata
    usage = lf.LMSamplingUsage(
        prompt_tokens=usage_metadata.prompt_token_count,
        completion_tokens=usage_metadata.candidates_token_count,
        total_tokens=usage_metadata.total_token_count,
    )
    return lf.LMSamplingResult(
        [
            # Scoring is not supported.
            lf.LMSample(
                self._generation_response_to_message(response), score=0.0
            ),
        ],
        usage=usage,
    )

  def _sample_text_generation_model(
      self, prompt: lf.Message
  ) -> lf.LMSamplingResult:
    """Samples a text generation model."""
    model = _VERTEXAI_MODEL_HUB.get_text_generation_model(self.model)
    predict_options = dict(
        temperature=self.sampling_options.temperature,
        top_k=self.sampling_options.top_k,
        top_p=self.sampling_options.top_p,
        max_output_tokens=self.sampling_options.max_tokens,
        stop_sequences=self.sampling_options.stop,
    )
    response = model.predict(prompt.text, **predict_options)
    return lf.LMSamplingResult([
        # Scoring is not supported.
        lf.LMSample(lf.AIMessage(response.text), score=0.0)
    ])


class _ModelHub:
  """Vertex AI model hub."""

  def __init__(self):
    self._generative_model_cache = {}
    self._text_generation_model_cache = {}

  def get_generative_model(
      self, model_id: str
  ) -> Any:  # generative_models.GenerativeModel:
    """Gets a generative model by model id."""
    model = self._generative_model_cache.get(model_id, None)
    if model is None:
      from google.cloud.aiplatform.vertexai.preview import generative_models  # pylint: disable=g-import-not-at-top
      model = generative_models.GenerativeModel(model_id)
      self._generative_model_cache[model_id] = model
    return model

  def get_text_generation_model(
      self, model_id: str
  ) -> Any:  # language_models.TextGenerationModel
    """Gets a text generation model by model id."""
    model = self._text_generation_model_cache.get(model_id, None)
    if model is None:
      from google.cloud.aiplatform.vertexai import language_models  # pylint: disable=g-import-not-at-top
      model = language_models.TextGenerationModel.from_pretrained(model_id)
      self._text_generation_model_cache[model_id] = model
    return model


_VERTEXAI_MODEL_HUB = _ModelHub()


class VertexAIGeminiPro1_5(VertexAI):  # pylint: disable=invalid-name
  """Vertex AI Gemini 1.5 Pro model."""

  model = 'gemini-1.5-pro-preview-0409'
  multimodal = True


class VertexAIGeminiPro1(VertexAI):  # pylint: disable=invalid-name
  """Vertex AI Gemini 1.0 Pro model."""

  model = 'gemini-1.0-pro'


class VertexAIGeminiPro1Vision(VertexAI):  # pylint: disable=invalid-name
  """Vertex AI Gemini 1.0 Pro model."""

  model = 'gemini-1.0-pro-vision'
  multimodal = True


class VertexAIPalm2(VertexAI):  # pylint: disable=invalid-name
  """Vertex AI PaLM2 text generation model."""

  model = 'text-bison'


class VertexAIPalm2_32K(VertexAI):  # pylint: disable=invalid-name
  """Vertex AI PaLM2 text generation model (32K context length)."""

  model = 'text-bison-32k'