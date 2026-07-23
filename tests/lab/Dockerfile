FROM rockylinux:9

# SSH server + narzędzia systemowe potrzebne dla Ansible
RUN dnf -y update && dnf -y install \
    openssh-server \
    sudo \
    python3 \
    python3-pip \
    python3-PyMySQL \
    chrony \
    firewalld \
    selinux-policy \
    libselinux-utils \
    policycoreutils \
    iproute \
    iputils \
    bind-utils \
    numactl \
    less \
    vim \
    lsof \
    socat \
    rsync \
    procps-ng \
    && dnf clean all


# Ansible user z sudo bez hasla
RUN useradd -m -s /bin/bash ansible && \
    passwd -d ansible && \
    echo 'ansible ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ansible

# SSH keys
RUN mkdir -p /home/ansible/.ssh && chmod 700 /home/ansible/.ssh && \
    chown ansible:ansible /home/ansible/.ssh
COPY --chown=ansible:ansible ssh_key.pub /home/ansible/.ssh/authorized_keys
RUN chmod 600 /home/ansible/.ssh/authorized_keys


# SSH host keys
RUN ssh-keygen -A

# SSH config: tylko klucz testowy, bez logowania hasłem i rootem
RUN printf 'PubkeyAuthentication yes\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitEmptyPasswords no\nPermitRootLogin no\n' \
    > /etc/ssh/sshd_config.d/99-lab-key-only.conf

# SELinux — kontenery Docker domyslnie nie enforce SELinux (zaleta: brak jadra z SELinux)
# F0 discovery zanotuje to jako "SELinux not available in container"

EXPOSE 22

# Start SSH (kontener musi dzialac jako daemon)
CMD ["/usr/sbin/sshd", "-D"]
