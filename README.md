Project structure:
app.py - hole backend + LLM + function handling
static/app.js - frontend
static/style.css - frontend styles
templates/index.html - frontend template

Problem:
When the Orchestrator calls the Plan function we don't get updated on the frontend about it.
Instead we get dumped everything from the hole process of the orchestrator when its over.

What needs to be fixed:
Every time the orchestrator creates a plan or fires the new agents we need to get updated on the frontend, not all at once at the end. To fix this we possible will need threading or different endpoint structure.
