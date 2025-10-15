# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY test_notification.py .
# Copy the entire project context into the container
COPY . .

# Entrypoint for the application
ENTRYPOINT ["python3"]
