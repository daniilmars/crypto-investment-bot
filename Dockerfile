# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project context into the container
COPY . .

# Make the run script executable
RUN chmod +x run.sh

# Set up the entrypoint script
ENTRYPOINT ["./run.sh"]
