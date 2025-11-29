# LLM Analysis Quiz

This repo implements the endpoint required for the LLM Analysis Quiz project. It exposes a POST endpoint `/task` that accepts quiz POST callbacks and solves quizzes by visiting the provided URL, extracting the required data, computing the answer and POSTing the answer to the submit endpoint described by the quiz page.

## How to run locally

1. Install system deps (Playwright browsers):
   ```bash
   pip install -r requirements.txt
   playwright install --with-deps chromium
   ```
2. Run the app:
   ```bash
   uvicorn main:app --reload
   ```

## Deploy to Hugging Face Spaces (Docker)

1. Create a new Space on Hugging Face and choose **Docker** as the runtime.
2. Push this repo (including `Dockerfile`) to the Space.
3. Configure hardware (CPU is usually fine). The Space will build the Docker image and run the app.

When the server receives a POST with a secret and URL, it will synchronously solve the quiz (within the allowed 3-minute window) and return JSON `200/400/403` responses per the instructions.
