# Tutor AI --- Development & Investigation Log

> **Purpose:** Maintain a reliable engineering record of the Tutor AI
> service, including architecture, configuration, implementation work,
> manual tests, investigations, findings, and outstanding tasks.
>
> **Accuracy rule:** Record only work that has actually been completed
> or verified. Keep failed tests and investigations; do not expose
> credentials or secrets.

------------------------------------------------------------------------

## 1. Project Overview

Tutor AI is the AI service used by the Tutor learning platform. Django
communicates with the FastAPI AI service, which coordinates tutor
conversations, retrieves lesson context, builds prompts, invokes the
configured model provider, and streams responses.

### 1.1 Development stack

  Area                            Technology / component
  ------------------------------- ----------------------------
  Platform backend                Django
  AI service                      FastAPI
  Language                        Python
  Development OS                  Windows 11
  Python version observed         3.13.9
  Chat generation                 Google Gemini
  Embedding generation            Ollama
  Embedding model                 `nomic-embed-text`
  Embedding dimensions observed   768
  Database                        Aiven PostgreSQL
  Vector search                   PostgreSQL with `pgvector`
  AI service port                 `8001`

These values describe the development environment recorded during the
project. Verify them if the environment changes.

------------------------------------------------------------------------

## 2. Repository Structure and AI Service Location

The AI service is maintained inside the repository's `ai/` directory.

``` text
tutor-latest/
└── ai/
    ├── run_server.py
    ├── requirements.txt
    ├── tutor_ai/
    │   ├── main.py
    │   ├── settings.py
    │   ├── tutor.py
    │   ├── retrieval.py
    │   ├── prompt_builder.py
    │   ├── prompts.py
    │   ├── api/
    │   │   └── tutor.py
    │   └── providers/
    │       ├── factory.py
    │       ├── gemini.py
    │       └── ollama.py
    └── prompts/
        └── tutor/
            └── v1.md
```

This is a high-level map of the files discussed and inspected during
development, not a complete directory listing.

------------------------------------------------------------------------

## 3. Starting the AI Service on Windows

### 3.1 Startup entry point

**File path:** `ai/run_server.py`

The project uses a dedicated startup script to launch the FastAPI
service on Windows.

The script configures the asyncio event-loop policy before starting
Uvicorn. This is important in the current Windows development
environment because asynchronous PostgreSQL operations using Psycopg can
encounter compatibility issues with the default Windows Proactor event
loop.

### 3.2 Startup procedure

Open PowerShell and navigate to the AI directory:

``` powershell
cd E:\tutor-gimini\tutor-latest\ai
```

Start the service:

``` powershell
python .\run_server.py
```

Expected local address:

``` text
http://127.0.0.1:8001
```

### 3.3 Startup notes

-   Use `python .\run_server.py` as the documented Windows startup
    method.
-   The startup script is located at **`ai/run_server.py`**, not in the
    repository root.
-   Do not replace this startup method with a direct Uvicorn command
    unless the required event-loop configuration is also applied.
-   Keep API keys, service tokens, and database credentials in
    environment configuration. Never commit secrets.

------------------------------------------------------------------------

## 4. Main AI Service Components

  ------------------------------------------------------------------------
  File                                 Responsibility
  ------------------------------------ -----------------------------------
  `ai/run_server.py`                   Configures the Windows event loop
                                       and starts the AI service.

  `ai/tutor_ai/main.py`                Creates and configures the FastAPI
                                       application and service
                                       dependencies.

  `ai/tutor_ai/settings.py`            Defines and loads application
                                       settings and environment variables.

  `ai/tutor_ai/api/tutor.py`           Defines the tutor API request
                                       handling and streaming route.

  `ai/tutor_ai/tutor.py`               Coordinates a tutor turn, context
                                       preparation, prompt construction,
                                       provider invocation, and response
                                       handling.

  `ai/tutor_ai/retrieval.py`           Retrieves lesson passages from the
                                       vector database.

  `ai/tutor_ai/prompt_builder.py`      Formats retrieved passages and
                                       builds messages for the chat
                                       provider.

  `ai/tutor_ai/prompts.py`             Loads and manages versioned prompt
                                       files.

  `ai/tutor_ai/providers/factory.py`   Selects and constructs the
                                       configured model provider.

  `ai/tutor_ai/providers/gemini.py`    Integrates Gemini for chat
                                       generation.

  `ai/tutor_ai/providers/ollama.py`    Integrates Ollama for local
                                       embedding generation.

  `ai/prompts/tutor/v1.md`             Contains the recorded version 1
                                       tutor prompt instructions.
  ------------------------------------------------------------------------

------------------------------------------------------------------------

## 5. Model Provider Configuration

### 5.1 Gemini --- chat generation

**Implementation:** `ai/tutor_ai/providers/gemini.py`

Gemini is integrated as the chat-generation provider. The provider
factory selects the configured provider based on application settings.

### 5.2 Ollama --- embedding generation

**Implementation:** `ai/tutor_ai/providers/ollama.py`

Ollama is used to generate embeddings with the `nomic-embed-text` model.
The observed embedding size during testing was **768 dimensions**.

### 5.3 Provider selection

**Implementation:** `ai/tutor_ai/providers/factory.py`

The provider factory constructs the provider selected by application
configuration. The development setup recorded here uses Gemini for chat
generation and Ollama for embeddings.

------------------------------------------------------------------------

## 6. Database and Retrieval

### 6.1 Storage

The development environment uses Aiven PostgreSQL with the `pgvector`
extension for vector storage and similarity search.

Lesson content is divided into chunks. Embeddings are generated for
those chunks and stored so relevant passages can be retrieved during a
tutor interaction.

### 6.2 Retrieval workflow

The inspected implementation follows this general flow:

1.  The tutor request provides lesson and conversation information.
2.  The service loads selected-lesson passages when pinned context is
    available.
3.  If context is missing or incomplete, the retrieval component
    searches for relevant passages.
4.  Selected-lesson passages are prioritized when a new conversation is
    initialized.
5.  Related course passages may be added when the context budget allows
    or further retrieval is required.
6.  Retrieved passages are passed to the prompt builder.

### 6.3 Course-wide retrieval diagnostic

A course-wide retrieval diagnostic recorded the following observations:

  Diagnostic item                                       Observed value
  --------------------------- ----------------------------------------
  Course ID                     `f0000000-0000-0000-0000-000000000001`
  Chunks observed                                                  283
  Lessons observed                                                  40
  Semantic results observed                                         30

**Diagnostic query:** "What is an intelligent system? Explain with a
practical example."

The results included passages from multiple lessons, including M30 and
M02. These values describe one diagnostic run and are not permanent
database totals.

### 6.4 Retrieval behavior under review

The inspected flow uses selected-lesson passages as primary context.
Depending on the request and similarity checks, course-wide retrieval
can provide additional passages.

The team may review whether fallback and additional-context behavior
should be adjusted. Preserve the selected lesson ID in the request flow
unless the project owner approves a change.

------------------------------------------------------------------------

## 7. Tutor API Endpoints

### 7.1 Tutor conversation

**Method and route:**

``` http
POST /v1/tutor/turns
```

The endpoint accepts a tutor-turn request and streams the generated
response using Server-Sent Events (SSE).

At a high level, the request includes lesson information, the student's
message, conversation history, and optional context or response limits.
The service prepares context, builds the prompt, calls the configured
chat provider, and streams the result.

### 7.2 Lesson indexing

**Method and route:**

``` http
PUT /v1/lessons/{lesson_id}/index
```

This endpoint is used to index lesson content. The indexing workflow
prepares lesson chunks and embeddings for storage and retrieval.

------------------------------------------------------------------------

## 8. Prompt Management

**Prompt file:** `ai/prompts/tutor/v1.md`\
**Prompt loader:** `ai/tutor_ai/prompts.py`

The prompt system uses versioned prompt files. The inspected registry
selects the highest available version for a prompt.

### 8.1 Recorded version 1 behavior

The inspected tutor prompt instructs the model to:

-   Teach only from the supplied lesson passages.
-   Tell the student when the lesson passages do not cover the answer
    and guide the student back to the lesson.
-   Keep the response within the configured word limit.
-   Explain in simple language.
-   Give one short example.
-   Cite the passages used.
-   Avoid going beyond the lesson or giving an unnecessary lecture.

### 8.2 Prompt versioning

The prompt-management implementation treats a prompt version as
immutable after use. When prompt behavior needs to change, create a new
version (for example, `v2.md`) rather than silently editing a version
already in use. Follow the project's prompt checksum and startup
behavior when making changes.

------------------------------------------------------------------------

## 9. Manual Testing Record

Use this section as the ongoing test history. Each entry should describe
what was actually run and observed.

### 9.1 Gemini single-request test

  -----------------------------------------------------------------------
  Field                               Record
  ----------------------------------- -----------------------------------
  Objective                           Verify that a tutor request
                                      receives a generated response
                                      through the Gemini integration.

  Observed result                     HTTP 200 was received; the response
                                      included a `[P1]` citation and a
                                      final answer.

  Status                              Passed in the recorded manual test.

  Evidence                            Terminal output shared during
                                      development.
  -----------------------------------------------------------------------

The example question in the recorded output concerned arithmetic
intensity and the roofline model.

### 9.2 Concurrent request test

  Metric                           Recorded result
  ------------------ -----------------------------
  Requests                                      10
  Successful                                    10
  Failed                                         0
  Total duration       Approximately 5.891 seconds
  Average duration     Approximately 4.535 seconds
  Fastest request       Approximately 3.35 seconds
  Slowest request      Approximately 5.476 seconds
  Status                Passed in the recorded run

These figures describe one recorded load-test run, not a performance
guarantee.

### 9.3 Embedding test

  Field               Recorded result
  ------------------- -----------------------------
  Provider            Ollama
  Model               `nomic-embed-text`
  Vectors generated   2
  Vector dimensions   768
  Status              Passed in the recorded test

### 9.4 Retrieval diagnostic

  ----------------------------------------------------------------------------
  Field                               Record
  ----------------------------------- ----------------------------------------
  Objective                           Inspect course-wide semantic retrieval
                                      results.

  Query                               "What is an intelligent system? Explain
                                      with a practical example."

  Course ID                           `f0000000-0000-0000-0000-000000000001`

  Chunks observed                     283

  Lessons observed                    40

  Semantic results                    30

  Status                              Diagnostic executed; behavior recorded
                                      for review.
  ----------------------------------------------------------------------------

### 9.5 Unit tests

A previous test run recorded **162 passed**. A separate earlier
Gemini-provider test run recorded **7 passed**.

These results are from earlier runs. Execute the current test suite
again before using them as evidence for a new pull request or release.

------------------------------------------------------------------------

## 10. Investigations and Findings

### 10.1 Short-response investigation

The tutor workflow was observed with a default `max_reply_tokens` value
of 250. It was changed to 600 during investigation, but answers were
still shorter than expected.

The investigation identified additional possible response-length
controls:

-   The incoming API request can provide `limits.max_reply_tokens`.
-   The tutor prompt contains a configured `$reply_words` limit.
-   The prompt also requests a short explanation and one short example.

**Status:** Investigation recorded. Re-test after reviewing all
applicable limits.

### 10.2 Lesson-only response behavior

The version 1 tutor prompt explicitly restricts answers to supplied
lesson passages. The tutor may therefore decline to answer when the
required information is not present in the retrieved context.

**Finding:** This behavior is consistent with the inspected prompt
instructions. Any change to the teaching policy should be reviewed and
introduced through the prompt versioning process.

### 10.3 Windows event-loop investigation

The AI service uses `ai/run_server.py` to configure the event loop
before starting the server.

**Finding:** The dedicated startup script is the documented Windows
startup path for the current Psycopg asynchronous database setup.

------------------------------------------------------------------------

## 11. Environment and Security Notes

-   Keep secrets in environment variables or approved secret storage.
-   Do not add `.env` files or credentials to Git.
-   Do not paste API keys, service tokens, database passwords, or
    complete connection strings into logs or documentation.
-   Use test data when validating API workflows.
-   Confirm that the database and model services are reachable before
    diagnosing application-level failures.
-   Do not expose internal AI-service endpoints publicly without the
    project's required authentication and network controls.

------------------------------------------------------------------------

## 12. Outstanding Items

The following items were not confirmed as completed in the recorded
development notes:

-   Re-run the full unit test suite against the current branch.
-   Re-run Gemini provider tests against the current implementation.
-   Re-test the tutor API after response-length configuration changes.
-   Review course-wide retrieval and fallback behavior with the project
    manager.
-   Record fresh manual-test evidence for the current code state.
-   Confirm deployment configuration separately from the local Windows
    setup.

Update this list as work progresses.

------------------------------------------------------------------------

## 13. Change Log

Add a dated entry whenever a meaningful implementation, configuration,
test, or investigation is completed.

  -----------------------------------------------------------------------------------------------
  Date           Area / file          Change or        Test / evidence             Status
                                      investigation
  -------------- -------------------- ---------------- --------------------------- --------------
  YYYY-MM-DD     `ai/run_server.py`   Documented       Start using                 Verify current
                                      Windows startup  `python .\run_server.py`.   behavior
                                      and event-loop
                                      configuration.

  YYYY-MM-DD     Gemini provider      Record the       Add the actual test command Add entry
                                      implementation   and result.
                                      or changes made.

  YYYY-MM-DD     Retrieval            Record retrieval Add query, dataset, and     Add entry
                                      changes or       observed result.
                                      diagnostic
                                      findings.
  -----------------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 14. Documentation Maintenance Standard

For every new task, record:

1.  **Objective** --- what needed to be done or investigated.
2.  **Files changed** --- exact repository-relative paths.
3.  **Implementation** --- what changed, without exposing secrets.
4.  **Manual test** --- exact command, endpoint, or test procedure.
5.  **Expected result** --- what should happen.
6.  **Actual result** --- what happened during the test.
7.  **Status** --- Passed, Failed, Blocked, or Pending.
8.  **Follow-up** --- remaining work or the next investigation.

Keep unsuccessful tests and investigations alongside successful work.
This document is intended to preserve the engineering history of Tutor
AI, including issues encountered, findings, and verified fixes.
