# Communication Helper App

## Overview

The **Communication Helper App** is an AI-assisted platform designed to evaluate and improve the communication skills of educators.

Instead of relying only on written tests or manual evaluation, the platform places educators in realistic classroom situations and asks them to respond through short videos. Their responses are processed and evaluated across areas such as teaching approach, technical understanding, and communication.

The system can also generate follow-up situations based on an educator's previous response, allowing the assessment to become more interactive and focused.

## How It Works

### 1. Educator Login

- The educator signs into the platform using their assigned ID and name.
- The system checks their evaluation history and assessment limits.
- Once eligible, they can begin an assessment.

### 2. Scenario Generation

When an assessment begins, the system uses **Google Gemini** to generate a classroom situation for the educator.

Scenarios can involve situations such as:

- Explaining a difficult concept to students
- Handling an unexpected classroom problem
- Responding to a technical issue during an activity
- Managing a classroom distraction
- Adapting an explanation for younger students

The purpose is to understand how the educator communicates and responds in a realistic teaching environment.

### 3. Video Response

The educator reads the scenario and records a **1–3 minute video response** explaining how they would handle the situation.

The submitted video is checked using a SHA-256 file hash to help identify duplicate submissions.

### 4. AI Processing

Once submitted, the video is processed in the background so the main application remains responsive.

The system:

1. Processes the video.
2. Generates a transcript.
3. Calculates basic speaking information such as word count and speaking pace.
4. Checks whether the response contains meaningful spoken content.
5. Evaluates the response against the assessment criteria.

The evaluation focuses on three main areas:

- **Pedagogy** — how effectively the educator approaches teaching and learning.
- **Technical Accuracy** — whether the technical explanation is accurate.
- **Communication** — how clearly and effectively the educator communicates.

### 5. Follow-Up Rounds

The assessment can continue after the initial response.

If additional evaluation is needed, the system generates a follow-up question or scenario based on the educator's previous response.

The educator records another response, which is processed and evaluated in the same way.

This allows the system to gather more information before reaching a final outcome.

### 6. Final Outcome

After the required assessment rounds are completed, the system produces a final assessment outcome.

Possible outcomes include:

- **Certified** — the educator has met the required assessment criteria.
- **Escalated** — the session requires human review because of an edge case, insufficient confidence, or another issue requiring administrator attention.

Evaluation results and feedback are stored for later review.

### 7. Admin Dashboard

Administrators can use the internal dashboard to:

- Review educator assessment sessions
- Inspect evaluation results
- Review AI-generated feedback
- Examine flagged submissions
- Review disputed assessments
- Audit the assessment process

## Technology

### User Interface

**Streamlit** provides the web interface for educators and administrators.

### Database

**PostgreSQL** stores educator information, assessment sessions, evaluation results, conversation history, and submission records.

### Background Processing

**Celery** and **Redis** handle longer-running tasks such as video processing and AI evaluation in the background, keeping the application responsive.

### AI

**Google Gemini**, accessed through the `google-genai` SDK, is used for:

- Generating assessment scenarios
- Processing and transcribing video responses
- Evaluating responses
- Generating follow-up questions
- Producing structured evaluation results

## Project Structure

The application is organized into separate components for the user interface, core application logic, services, database operations, and background processing.

This separation keeps the different parts of the application easier to develop, test, and maintain.

The project is actively developed through the `development` branch. The `main` branch is reserved for stable versions that have been tested and are ready for demonstrations or use.
