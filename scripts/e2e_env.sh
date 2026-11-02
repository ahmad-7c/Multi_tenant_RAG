# Environment for the end-to-end stack. Sourced by scripts/e2e_api.sh.
#
# A SEPARATE database and FAKE providers. Two reasons the dev database
# cannot be used: the startup guard refuses to serve queries when the
# stored vectors came from a different embedding model than the
# configured one (dev holds gemini vectors, e2e runs fake), and a test
# that deletes documents must never be pointed at real data.
export DATABASE_URL="postgresql://documind:documind@localhost:5434/documind_e2e"
export ENV="development"
export EMBEDDING_PROVIDER="fake"
# MUST match what FakeEmbeddings stamps on each row. Setting the
# provider without the model name left the startup guard comparing
# stored "fake-embedding" against configured "gemini-embedding-001",
# so /health reported embedding_model_mismatch and every query
# endpoint returned 503 -- which surfaced as chat tests timing out.
export EMBEDDING_MODEL="fake-embedding"
export LLM_PROVIDER="fake"
export RERANKER_PROVIDER="fake"
# Ingestion runs inline, so an upload is readable the moment the
# request returns and the test does not race a worker process.
export CELERY_TASK_ALWAYS_EAGER="true"
# The gate would otherwise refuse everything the fake reranker scores.
export VECTOR_SIMILARITY_FLOOR="0.0"
# FakeReranker scores by word overlap with the question, so this has
# to sit between an off-topic question (~0.2, shared stopwords) and an
# on-topic one (~0.6+). At 0.05 nothing was ever refused and the
# refusal test could not pass.
export CONFIDENCE_THRESHOLD="0.4"
# Uploads land somewhere disposable.
export STORAGE_LOCAL_ROOT="uploads-e2e"
# Rate limiting stays ON -- test 7 asserts the 429 path -- but the
# limits are raised so the suite itself does not trip them.
# BURST matters as much as the per-minute rate: the defaults are 3 for
# register and 5 for login, so a suite that registers six orgs trips
# them on the fourth and the failures look like broken auth.
export RATE_LIMIT_ANSWER_PER_MINUTE="600"
export RATE_LIMIT_ANSWER_BURST="200"
export RATE_LIMIT_REGISTER_PER_MINUTE="600"
export RATE_LIMIT_REGISTER_BURST="200"
export RATE_LIMIT_LOGIN_PER_MINUTE="600"
export RATE_LIMIT_LOGIN_BURST="200"
export RATE_LIMIT_DEFAULT_PER_MINUTE="3000"
export RATE_LIMIT_DEFAULT_BURST="600"
export CORS_ALLOW_ORIGINS=""
