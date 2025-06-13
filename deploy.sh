#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Print with color
print_step() {
    echo -e "${GREEN}[+] $1${NC}"
}

print_error() {
    echo -e "${RED}[!] $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}[*] $1${NC}"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    print_error "Please run as root"
    exit 1
fi

# Configuration
DOMAIN="template.online"
APP_DIR="/var/www/$DOMAIN"
PYTHON_VERSION="3.11"
YOUTUBE_API_KEY="YOUR_API_KEY_HERE"  # Replace with your API key

# Update system
print_step "Updating system packages..."
apt update && apt upgrade -y

# Install required packages
print_step "Installing required packages..."
apt install -y python3-pip python3-venv nginx certbot python3-certbot-nginx ffmpeg

# Create application directory
print_step "Creating application directory..."
mkdir -p $APP_DIR
cd $APP_DIR

# Create virtual environment
print_step "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
print_step "Installing Python packages..."
pip install --upgrade pip
pip install fastapi uvicorn yt-dlp google-api-python-client pydantic python-multipart requests aiofiles python-dotenv

# Create necessary directories
print_step "Creating application directories..."
mkdir -p downloads cache

# Create systemd service file
print_step "Creating systemd service..."
cat > /etc/systemd/system/template-api.service << EOF
[Unit]
Description=Template Online API
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=$APP_DIR
Environment="PATH=$APP_DIR/venv/bin"
Environment="YOUTUBE_API_KEY=$YOUTUBE_API_KEY"
ExecStart=$APP_DIR/venv/bin/python main.py

[Install]
WantedBy=multi-user.target
EOF

# Create Nginx configuration
print_step "Configuring Nginx..."
cat > /etc/nginx/sites-available/$DOMAIN << EOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Enable Nginx site
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Set up SSL
print_step "Setting up SSL certificate..."
certbot --nginx -d $DOMAIN -d www.$DOMAIN --non-interactive --agree-tos --email admin@$DOMAIN

# Set correct permissions
print_step "Setting up permissions..."
chown -R www-data:www-data $APP_DIR
chmod -R 755 $APP_DIR

# Create backup script
print_step "Setting up backup system..."
cat > $APP_DIR/backup.sh << 'EOF'
#!/bin/bash
backup_dir="/var/backups/template.online"
mkdir -p $backup_dir
tar -czf $backup_dir/backup-$(date +%Y%m%d).tar.gz /var/www/template.online
find $backup_dir -type f -mtime +7 -delete
EOF

chmod +x $APP_DIR/backup.sh

# Add backup to crontab
(crontab -l 2>/dev/null; echo "0 0 * * * $APP_DIR/backup.sh") | crontab -

# Configure firewall
print_step "Configuring firewall..."
ufw allow 80
ufw allow 443
ufw --force enable

# Start services
print_step "Starting services..."
systemctl daemon-reload
systemctl enable template-api
systemctl start template-api
systemctl restart nginx

# Create update script
print_step "Creating update script..."
cat > $APP_DIR/update.sh << 'EOF'
#!/bin/bash
cd /var/www/template.online
source venv/bin/activate
git pull
pip install -r requirements.txt
systemctl restart template-api
EOF

chmod +x $APP_DIR/update.sh

print_step "Deployment completed successfully!"
print_warning "Please make sure to:"
print_warning "1. Replace YOUR_API_KEY_HERE with your actual YouTube API key"
print_warning "2. Update your DNS records to point to this server"
print_warning "3. Test the API endpoints"
print_warning "4. Monitor the logs: journalctl -u template-api -f" 