#!/usr/bin/env python3
# Shebang: tells the system to run this script with Python 3

# Module docstring describing the purpose and usage of the script
r"""Create a Vertex AI Search test data store for integration testing.

Provisions the GCS bucket, uploads the fixture JSONL, creates a NO_CONTENT
structured data store, imports the documents, and waits for indexing.

Usage
-----
    # Authenticate first (or rely on the GCE service account in CI)
    gcloud auth application-default login

    # Run from the repo root
    uv run python -m scripts.create_test_datastore \\
        --bucket <globally-unique-bucket-name> \\
        [--project agentic-ai-evaluation-bootcamp] \\
        [--datastore-id vertex-search-integration-test]

After the script finishes it prints the VERTEX_AI_DATASTORE_ID value
to add to your .env file.
"""

# Import standard library modules
import argparse          # Command-line argument parsing
import base64            # Base64 encoding for document content
import json              # JSON serialization/deserialization
import sys               # System-specific parameters (stderr, exit)
import time              # Sleep and timing functions
from pathlib import Path # Object-oriented filesystem paths

# Import Google authentication and request modules
import google.auth                 # Google Auth library for credentials
import google.auth.transport.requests  # Authorized HTTP session

# Define base URLs for Google APIs
DISCOVERY_ENGINE_BASE = "https://discoveryengine.googleapis.com/v1"  # Vertex AI Search API
STORAGE_BASE = "https://storage.googleapis.com/storage/v1"           # Cloud Storage API (metadata)
STORAGE_UPLOAD_BASE = "https://storage.googleapis.com/upload/storage/v1"  # Cloud Storage upload endpoint

# Path to the fixture file (relative to the repository root)
FIXTURE_PATH = Path(__file__).parent.parent / "aieng-eval-agents" / "tests" / "fixtures" / "vertex_test_data.jsonl"


def get_session() -> google.auth.transport.requests.AuthorizedSession:
    """Return an authorised requests session using Application Default Credentials."""
    # Obtain default credentials with Cloud Platform scope
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    # Create an authorized session that automatically adds authentication headers
    return google.auth.transport.requests.AuthorizedSession(credentials)


def create_bucket(session, project: str, bucket: str) -> None:
    """Create a GCS bucket in us-central1, skipping if it already exists."""
    # Build URL for bucket creation
    url = f"{STORAGE_BASE}/b?project={project}"
    # Bucket configuration: name, location, storage class
    body = {"name": bucket, "location": "us-central1", "storageClass": "STANDARD"}
    # Send POST request to create bucket
    resp = session.post(url, json=body)
    if resp.status_code == 409:
        # Bucket already exists (conflict) – skip
        print(f"  Bucket gs://{bucket} already exists — skipping creation.")
    elif resp.status_code in (200, 201):
        # Successfully created
        print(f"  Created bucket gs://{bucket}")
    else:
        # Other error – print and raise
        print(f"  Error creating bucket: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def transform_to_content_required(source_path: Path) -> bytes:
    """Transform participant-format JSONL to Discovery Engine CONTENT_REQUIRED format.

    Participant format (flat):
        {"id": "x", "text": "...", "title": "...", "category": "..."}

    Discovery Engine CONTENT_REQUIRED format:
        {
            "id": "x",
            "content": {"mimeType": "text/plain", "rawBytes": "<base64>"},
            "structData": {...}
        }

    The ``text`` field becomes the indexed document content (stored as base64 rawBytes).
    All other fields (except ``id``) become metadata in ``structData``.
    """
    # Raise error if source file doesn't exist
    if not source_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {source_path}")

    output_lines = []  # List to collect transformed JSON strings
    # Read file and split into lines (strip trailing whitespace)
    for raw_line in source_path.read_text(encoding="utf-8").strip().splitlines():
        # Parse JSON line into dictionary
        row = json.loads(raw_line)
        # Extract and remove the 'id' field
        doc_id = row.pop("id")
        # Extract and remove the 'text' field (document content)
        text = row.pop("text", "")
        # Build new document in CONTENT_REQUIRED format
        doc = {
            "id": doc_id,  # Unique document ID
            "content": {
                "mimeType": "text/plain",  # Plain text content type
                # Encode text as base64 (required by the API)
                "rawBytes": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            },
            "structData": row,  # Remaining fields (title, category, etc.) become metadata
        }
        # Append JSON string to output list
        output_lines.append(json.dumps(doc))

    # Join lines with newline and encode as UTF-8 bytes
    return "\n".join(output_lines).encode("utf-8")


def upload_fixture(session, bucket: str, object_name: str) -> None:
    """Transform and upload the JSONL fixture to GCS in CONTENT_REQUIRED format."""
    # Transform the fixture file to the required format (returns bytes)
    payload = transform_to_content_required(FIXTURE_PATH)
    # Build upload URL with media upload type
    url = f"{STORAGE_UPLOAD_BASE}/b/{bucket}/o?uploadType=media&name={object_name}"
    # Send POST request with binary payload
    resp = session.post(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    # Raise exception if upload failed
    resp.raise_for_status()
    print(f"  Transformed and uploaded {FIXTURE_PATH.name} → gs://{bucket}/{object_name}")


def create_datastore(session, project: str, datastore_id: str) -> None:
    """Create a NO_CONTENT structured search data store, skipping if it exists."""
    # Build URL for data store creation
    url = (
        f"{DISCOVERY_ENGINE_BASE}/projects/{project}/locations/global"
        f"/collections/default_collection/dataStores?dataStoreId={datastore_id}"
    )
    # Data store configuration
    body = {
        "displayName": "Vertex Search Integration Test",  # Human-readable name
        "industryVertical": "GENERIC",                    # Generic content type
        "contentConfig": "CONTENT_REQUIRED",              # Require document content
        "solutionTypes": ["SOLUTION_TYPE_SEARCH"],        # Enable search functionality
    }
    # Send POST request
    resp = session.post(url, json=body)
    if resp.status_code == 409:
        # Data store already exists – skip
        print(f"  Data store '{datastore_id}' already exists — skipping creation.")
    elif resp.status_code in (200, 201):
        # Successfully created
        print(f"  Created data store '{datastore_id}'")
        # Allow a moment for the data store to become fully ready
        time.sleep(5)
    else:
        # Error – print and raise
        print(f"  Error creating data store: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def import_documents(session, project: str, datastore_id: str, gcs_uri: str) -> str:
    """Trigger an async document import from GCS. Returns the operation name."""
    # Build URL for the import operation
    url = (
        f"{DISCOVERY_ENGINE_BASE}/projects/{project}/locations/global"
        f"/collections/default_collection/dataStores/{datastore_id}"
        f"/branches/default_branch/documents:import"
    )
    # Request body: GCS source and reconciliation mode
    body = {
        "gcsSource": {
            "inputUris": [gcs_uri],  # URI of the JSONL file in GCS
            # "document" matches our JSONL format:
            # {id, content:{mimeType,rawBytes}, structData:{...}}
            "dataSchema": "document",
        },
        # FULL replaces all existing documents, keeping the test store deterministic
        "reconciliationMode": "FULL",
    }
    # Send POST request to start the async import
    resp = session.post(url, json=body)
    resp.raise_for_status()
    # Extract the operation name from the response
    operation_name = resp.json()["name"]
    print(f"  Import operation started: {operation_name}")
    return operation_name


def wait_for_operation(
    session,
    operation_name: str,
    timeout_sec: int = 600,
    poll_interval: int = 15,
) -> dict:
    """Poll the operation until it is done or the timeout is reached."""
    # Build URL to check operation status
    url = f"{DISCOVERY_ENGINE_BASE}/{operation_name}"
    start = time.time()          # Record start time
    deadline = start + timeout_sec  # Calculate absolute deadline

    # Loop until deadline
    while time.time() < deadline:
        # Send GET request to check status
        resp = session.get(url)
        resp.raise_for_status()
        op = resp.json()

        # Check if operation is complete
        if op.get("done"):
            if "error" in op:
                # Operation failed with an error
                raise RuntimeError(f"Import operation failed: {op['error']}")
            # Check per-document failure count in metadata
            metadata = op.get("metadata", {})
            failure_count = int(metadata.get("failureCount", 0))
            total_count = int(metadata.get("totalCount", 0))
            if failure_count > 0:
                # Some documents failed – try to get first error message
                samples = op.get("response", {}).get("errorSamples", [])
                sample_msg = samples[0]["message"] if samples else "unknown error"
                raise RuntimeError(
                    f"Import completed but {failure_count}/{total_count} documents failed. First error: {sample_msg}"
                )
            print(f"  Indexing complete — {total_count} documents imported.")
            return op

        # Operation still in progress – log progress and sleep
        elapsed = int(time.time() - start)
        print(f"  Indexing in progress… ({elapsed}s elapsed, checking again in {poll_interval}s)")
        time.sleep(poll_interval)

    # Timeout reached without completion
    raise TimeoutError(f"Operation did not complete within {timeout_sec}s: {operation_name}")


def main() -> None:
    """Parse CLI arguments and provision the Vertex AI Search test data store."""
    # Create argument parser with description
    parser = argparse.ArgumentParser(
        description="Provision a Vertex AI Search test data store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Add --project argument (default: agentic-ai-evaluation-bootcamp)
    parser.add_argument(
        "--project",
        default="agentic-ai-evaluation-bootcamp",
        help="GCP project ID (default: agentic-ai-evaluation-bootcamp)",
    )
    # Add --bucket argument (required)
    parser.add_argument(
        "--bucket",
        required=True,
        help="GCS bucket name for staging the import file (must be globally unique)",
    )
    # Add --datastore-id argument (optional with default)
    parser.add_argument(
        "--datastore-id",
        default="vertex-search-integration-test",
        help="Vertex AI Search data store ID (default: vertex-search-integration-test)",
    )
    # Parse command-line arguments
    args = parser.parse_args()

    # Build GCS object name and full URI
    gcs_object = "vertex-search-test/vertex_test_data.jsonl"
    gcs_uri = f"gs://{args.bucket}/{gcs_object}"
    # Build data store resource path for display
    datastore_resource = (
        f"projects/{args.project}/locations/global/collections/default_collection/dataStores/{args.datastore_id}"
    )

    # Print summary of what will be provisioned
    print("Vertex AI Search — test data store provisioning")
    print("=" * 55)
    print(f"  Project:    {args.project}")
    print(f"  Bucket:     gs://{args.bucket}")
    print(f"  Data store: {datastore_resource}")
    print()

    # Get authenticated session
    session = get_session()

    # Step 1: Create GCS bucket
    print("Step 1/5  Creating GCS bucket…")
    create_bucket(session, args.project, args.bucket)

    # Step 2: Upload fixture data to GCS
    print("Step 2/5  Uploading fixture data to GCS…")
    upload_fixture(session, args.bucket, gcs_object)

    # Step 3: Create Vertex AI Search data store
    print("Step 3/5  Creating Vertex AI Search data store…")
    create_datastore(session, args.project, args.datastore_id)

    # Step 4: Trigger document import
    print("Step 4/5  Importing documents…")
    operation_name = import_documents(session, args.project, args.datastore_id, gcs_uri)

    # Step 5: Wait for indexing to complete
    print("Step 5/5  Waiting for indexing (may take several minutes)…")
    wait_for_operation(session, operation_name)

    # Print final instructions for .env file
    print()
    print("=" * 55)
    print("Done! Add this to your .env file:")
    print()
    print(f'VERTEX_AI_DATASTORE_ID="{datastore_resource}"')
    print("=" * 55)


# Entry point: run main() only if script is executed directly
if __name__ == "__main__":
    main()
