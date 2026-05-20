# Use the official uv Python 3.12 slim image from GitHub Container Registry
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Expose port 8888 for Jupyter notebook or other services
EXPOSE 8888

# Install system dependencies
RUN apt-get update \
    # Update package list
    && apt-get install -y \
    sudo \               # Allow non-root user to run privileged commands
    curl \               # Command-line tool for transferring data
    git \                # Version control system
    jq \                 # Lightweight JSON processor
    tar \                # Archiving utility
    unzip \              # Extract ZIP archives
    ca-certificates \    # SSL certificate authorities for HTTPS
    build-essential \    # Compilers and make for building native code
    # Clean up apt cache to reduce image size
    && rm -rf /var/lib/apt/lists/*

# !!IMPORTANT!!
# THIS SECTION SHOULD NOT BE MODIFIED AS
# IT IS USED TO MAKE THIS IMAGE COMPATIBLE WITH CODER
#######################################################################
# Define the username for the non-root user
ARG USER=coder

# Create user with sudo access, no home directory yet, bash as shell
RUN useradd --groups sudo --no-create-home --shell /bin/bash ${USER} \
    # Allow passwordless sudo for the user
    && echo "${USER} ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/${USER} \
    # Set correct permissions for the sudoers file
    && chmod 0440 /etc/sudoers.d/${USER}

# Switch to the non-root user
USER ${USER}

# Set working directory to the user's home
WORKDIR /home/${USER}
########################################################################

# Copy the entire project source code into the container, preserving ownership
COPY --chown=${USER}:${USER} . /home/${USER}/aieng-template-implementation

# Start the container and run the project setup script
CMD ["bash", "aieng-template-implementation/scripts/setup.sh"]
