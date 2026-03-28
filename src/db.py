from cassandra.cluster import Cluster, Session
from cassandra.auth import PlainTextAuthProvider
from cassandra.query import dict_factory
from cassandra.policies import DCAwareRoundRobinPolicy
from typing import Optional, List, Dict, Any
import os
from dotenv import load_dotenv
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Database:
    """Database connection manager"""
    
    _instance = None
    _session: Optional[Session] = None
    _cluster: Optional[Cluster] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def connect(self):
        """Connect to Database"""
        try:
            hosts = os.getenv("SCYLLA_HOSTS", "localhost").split(",")
            port = int(os.getenv("SCYLLA_PORT", "9042"))
            
            # Create cluster connection
            self._cluster = Cluster(
                hosts,
                port=port,
                load_balancing_policy=DCAwareRoundRobinPolicy(local_dc='datacenter1')
            )
            
            self._session = self._cluster.connect()
            self._session.row_factory = dict_factory
            
            # Initialize keyspace and tables
            self._init_keyspace()
            self._init_tables()
            
            logger.info("Successfully connected to Database")
            
        except Exception as e:
            logger.error(f"Failed to connect to Database: {e}")
            raise
    
    def _init_keyspace(self):
        """Create keyspace if not exists"""
        keyspace = os.getenv("SCYLLA_KEYSPACE", "auth_app")
        
        # Check if keyspace exists
        rows = self._session.execute(
            "SELECT keyspace_name FROM system_schema.keyspaces WHERE keyspace_name = %s",
            [keyspace]
        )
        
        if not rows:
            # Create keyspace with NetworkTopologyStrategy for production
            create_keyspace_query = f"""
                CREATE KEYSPACE IF NOT EXISTS {keyspace}
                WITH replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': 1
                }}
            """
            self._session.execute(create_keyspace_query)
            logger.info(f"Created keyspace: {keyspace}")
        
        # Use the keyspace
        self._session.set_keyspace(keyspace)
    
    def _init_tables(self):
        """Create tables if not exists"""
        
        # Users table
        create_users_table = """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                email TEXT,
                hashed_password TEXT,
                is_active BOOLEAN,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """
        self._session.execute(create_users_table)
        
        # Email index for faster email lookups
        create_email_index = """
            CREATE INDEX IF NOT EXISTS users_email_idx ON users (email)
        """
        self._session.execute(create_email_index)
        
        # Tokens table for refresh tokens (optional)
        create_tokens_table = """
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                "token" TEXT PRIMARY KEY,
                username TEXT,
                expires_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """
        self._session.execute(create_tokens_table)
        
        # Failed login attempts table
        create_failed_logins_table = """
            CREATE TABLE IF NOT EXISTS failed_logins (
                username TEXT,
                attempt_time TIMESTAMP,
                ip_address TEXT,
                PRIMARY KEY (username, attempt_time)
            ) WITH CLUSTERING ORDER BY (attempt_time DESC)
        """
        self._session.execute(create_failed_logins_table)

        # Files table - stores user workspace files/folders
        create_files_table = """
            CREATE TABLE IF NOT EXISTS files (
                user_id UUID,
                file_id UUID,
                parent_id UUID,
                type TEXT,
                name TEXT,
                content TEXT,
                language TEXT,
                is_expanded BOOLEAN,
                position INT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                PRIMARY KEY (user_id, file_id)
            )
        """
        self._session.execute(create_files_table)
        
        logger.info("Tables created/verified successfully")
    
    def get_session(self) -> Session:
        """Get database session"""
        if self._session is None:
            self.connect()
        return self._session
    
    def close(self):
        """Close database connection"""
        if self._session:
            self._session.shutdown()
        if self._cluster:
            self._cluster.shutdown()
        logger.info("Database connection closed")

class UserRepository:
    """User data access layer"""
    
    def __init__(self, db: Database):
        self.db = db
        self.session = db.get_session()
    
    def create_user(self, username: str, email: str, hashed_password: str) -> Dict[str, Any]:
        """Create a new user"""
        from datetime import datetime
        
        query = """
            INSERT INTO users (username, email, hashed_password, is_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        
        now = datetime.utcnow()
        self.session.execute(
            query,
            (username, email, hashed_password, True, now, now)
        )
        
        return self.get_user(username)
    
    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username"""
        query = "SELECT * FROM users WHERE username = %s"
        rows = self.session.execute(query, (username,))
        
        if rows:
            return rows.one()
        return None
    
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email"""
        query = "SELECT * FROM users WHERE email = %s ALLOW FILTERING"
        rows = self.session.execute(query, (email,))
        
        if rows:
            return rows.one()
        return None
    
    def update_user(self, username: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update user data"""
        from datetime import datetime
        
        if not kwargs:
            return self.get_user(username)
        
        # Build update query dynamically
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in ['email', 'hashed_password', 'is_active']:
                updates.append(f"{key} = %s")
                values.append(value)
        
        if not updates:
            return self.get_user(username)
        
        # Add updated_at
        updates.append("updated_at = %s")
        values.append(datetime.utcnow())
        values.append(username)
        
        query = f"UPDATE users SET {', '.join(updates)} WHERE username = %s"
        self.session.execute(query, values)
        
        return self.get_user(username)
    
    def delete_user(self, username: str) -> bool:
        """Delete user"""
        query = "DELETE FROM users WHERE username = %s"
        self.session.execute(query, (username,))
        return True
    
    def list_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List users with pagination"""
        query = "SELECT * FROM users LIMIT %s"
        rows = self.session.execute(query, (limit,))
        return list(rows)
    
    def record_failed_login(self, username: str, ip_address: str = None):
        """Record failed login attempt"""
        from datetime import datetime
        
        query = """
            INSERT INTO failed_logins (username, attempt_time, ip_address)
            VALUES (%s, %s, %s)
        """
        self.session.execute(query, (username, datetime.utcnow(), ip_address))
    
    def get_recent_failed_logins(self, username: str, minutes: int = 15) -> int:
        """Get number of failed login attempts in last X minutes"""
        from datetime import datetime, timedelta
        
        cutoff_time = datetime.utcnow() - timedelta(minutes=minutes)
        
        query = """
            SELECT COUNT(*) as count 
            FROM failed_logins 
            WHERE username = %s AND attempt_time > %s
        """
        rows = self.session.execute(query, (username, cutoff_time))
        
        if rows:
            return rows.one()['count']
        return 0
    
    def clear_failed_logins(self, username: str):
        """Clear failed login attempts for user"""
        query = "DELETE FROM failed_logins WHERE username = %s"
        self.session.execute(query, (username,))

# Global database instance
db = Database()

def init_database():
    """Initialize database connection"""
    db.connect()
    return UserRepository(db)

def get_user_repository() -> UserRepository:
    """Dependency injection for user repository"""
    return UserRepository(db)
