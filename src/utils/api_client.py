import requests
import os
from dotenv import load_dotenv

load_dotenv()

class HLLAPIClient:
    def __init__(self):
        self.servers = self._load_servers()
        # Shared credentials
        self.shared_api_token = os.getenv('CRCON_API_TOKEN')
        self.shared_rcon_host = os.getenv('RCON_HOST')
        self.shared_rcon_port = os.getenv('RCON_PORT')
        self.shared_rcon_password = os.getenv('RCON_PASSWORD')
        
        # Fetch server names from API
        self._fetch_server_names()
    
    def _load_servers(self):
        """Load server configurations from environment variables"""
        servers = []
        
        # Check for single server format first (backward compatibility)
        if os.getenv('CRCON_BASE_URL'):
            servers.append({
                "name": "Loading...",  # Will be updated from API
                "crcon_url": os.getenv('CRCON_BASE_URL')
            })
            return servers
        
        # Check for multiple servers (SERVER1_URL, SERVER2_URL, etc.)
        server_num = 1
        while True:
            url_key = f'SERVER{server_num}_URL'
            url = os.getenv(url_key)
            
            if not url:
                break
                
            servers.append({
                "name": "Loading...",  # Will be updated from API
                "crcon_url": url
            })
            server_num += 1
        
        return servers
    
    def _fetch_server_names(self):
        """Fetch server names from each server's API"""
        for i, server in enumerate(self.servers):
            try:
                # Use the correct API endpoint: get_name
                url = f"{server['crcon_url']}/api/get_name"
                headers = {
                    'Authorization': f'Bearer {self.shared_api_token}',
                    'Content-Type': 'application/json'
                }
                
                response = requests.get(url, headers=headers, timeout=5)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('result'):
                        self.servers[i]['name'] = data['result']
                        print(f"Server {i+1} name: {data['result']}")
                    else:
                        # Fallback to a generic name with server number
                        self.servers[i]['name'] = f"HLL Server {i+1}"
                        print(f"Server {i+1}: Using fallback name")
                else:
                    self.servers[i]['name'] = f"HLL Server {i+1}"
                    print(f"Server {i+1}: API error, using fallback name")
                    
            except Exception as e:
                # Fallback name if API call fails
                self.servers[i]['name'] = f"HLL Server {i+1}"
                print(f"Server {i+1}: Failed to fetch name ({str(e)}), using fallback")
    
    def get_current_map(self, server_index):
        """Get the current map for a specific server"""
        if server_index >= len(self.servers):
            return "Unknown"
        
        server = self.servers[server_index]
        
        try:
            url = f"{server['crcon_url']}/api/get_map"
            headers = {
                'Authorization': f'Bearer {self.shared_api_token}',
                'Content-Type': 'application/json'
            }
            
            response = requests.get(url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('result'):
                    # The API returns a Layer object with id, name, etc.
                    map_data = data['result']
                    if isinstance(map_data, dict):
                        return map_data.get('pretty_name', map_data.get('id', 'Unknown'))
                    else:
                        return str(map_data)
                    
            return "Unknown"
                    
        except Exception as e:
            print(f"Failed to get current map for server {server_index}: {str(e)}")
            return "Unknown"
    
    def get_servers(self):
        """Get list of available servers"""
        return [(i, server["name"]) for i, server in enumerate(self.servers)]
    
    def set_map(self, server_index, map_id):
        """Change the server map using the CRCON API"""
        if server_index >= len(self.servers):
            return False, "Invalid server index"
        
        server = self.servers[server_index]
        
        try:
            url = f"{server['crcon_url']}/api/set_map"
            headers = {
                'Authorization': f'Bearer {self.shared_api_token}',
                'Content-Type': 'application/json'
            }
            data = {"map_name": map_id}
            
            response = requests.post(url, json=data, headers=headers)
            
            if response.status_code == 200:
                return True, f"Successfully changed map to {map_id} on {server['name']}"
            else:
                return False, f"Failed to change map on {server['name']}: {response.status_code} - {response.text}"
                
        except Exception as e:
            return False, f"Error calling API for {server['name']}: {str(e)}"


    def get_server_name(self, server_index):
        """Get server name by index"""
        if server_index < len(self.servers):
            return self.servers[server_index]["name"]
        return "Unknown Server"