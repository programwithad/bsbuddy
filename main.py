from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Dict, List
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

from database import engine, get_db, Base
from models import User, Group, GroupMember, Message, DirectMessage

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic schemas ---

class LoginRequest(BaseModel):
    token: str  # Google JWT credential


class JoinGroupRequest(BaseModel):
    user_id: int
    group_name: str
    group_type: str  # "city" or "train"


# --- WebSocket connection manager ---

class ConnectionManager:
    def __init__(self):
        # group_id -> list of (websocket, user_id) tuples
        self.active: Dict[int, List[tuple]] = {}

    async def connect(self, ws: WebSocket, group_id: int, user_id: int):
        await ws.accept()
        if group_id not in self.active:
            self.active[group_id] = []
        self.active[group_id].append((ws, user_id))

    def disconnect(self, ws: WebSocket, group_id: int):
        if group_id in self.active:
            self.active[group_id] = [
                (w, u) for w, u in self.active[group_id] if w != ws
            ]

    async def broadcast(self, group_id: int, message: dict):
        if group_id in self.active:
            data = json.dumps(message)
            for ws, _ in self.active[group_id]:
                await ws.send_text(data)


manager = ConnectionManager()


# --- REST endpoints ---

@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    # Verify Google JWT token
    try:
        idinfo = id_token.verify_oauth2_token(
            req.token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    google_id = idinfo["sub"]
    email = idinfo.get("email", "")
    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")

    # Only allow IITM DS students
    # if not email.endswith("@ds.study.iitm.ac.in"):
    #     raise HTTPException(status_code=403, detail="Only IITM-BS Students are allowed")

    # Find by google_id or create
    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = User(google_id=google_id, name=name, email=email, picture=picture)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update name/picture in case they changed on Google side
        user.name = name
        user.picture = picture
        db.commit()

    return {"id": user.id, "name": user.name, "email": user.email, "picture": user.picture}
   

@app.post("/join-group")
def join_group(req: JoinGroupRequest, db: Session = Depends(get_db)):
    # Find or create group
    group = db.query(Group).filter(
        Group.name == req.group_name, Group.type == req.group_type
    ).first()
    if not group:
        group = Group(name=req.group_name, type=req.group_type)
        db.add(group)
        db.commit()
        db.refresh(group)

    # Check if already a member
    existing = db.query(GroupMember).filter(
        GroupMember.user_id == req.user_id,
        GroupMember.group_id == group.id,
    ).first()
    if not existing:
        db.add(GroupMember(user_id=req.user_id, group_id=group.id))
        db.commit()

    return {"group_id": group.id, "group_name": group.name, "group_type": group.type}


@app.get("/groups/{user_id}")
def get_user_groups(user_id: int, db: Session = Depends(get_db)):
    memberships = db.query(GroupMember).filter(GroupMember.user_id == user_id).all()
    groups = []
    for m in memberships:
        g = db.query(Group).filter(Group.id == m.group_id).first()
        if g:
            groups.append({"id": g.id, "name": g.name, "type": g.type})
    return groups


@app.get("/messages/{group_id}")
def get_messages(group_id: int, db: Session = Depends(get_db)):
    msgs = (
        db.query(Message)
        .filter(Message.group_id == group_id)
        .order_by(Message.timestamp.asc())
        .all()
    )
    result = []
    for m in msgs:
        sender = db.query(User).filter(User.id == m.sender_id).first()
        result.append({
            "id": m.id,
            "sender_name": sender.name if sender else "Unknown",
            "sender_id": m.sender_id,
            "content": m.content,
            "timestamp": m.timestamp.isoformat(),
        })
    return result


# --- WebSocket endpoint ---

@app.websocket("/ws/{group_id}")
async def websocket_endpoint(ws: WebSocket, group_id: int):
    # Get user_id from query param
    user_id = int(ws.query_params.get("user_id", 0))
    await manager.connect(ws, group_id, user_id)
    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            content = payload.get("content", "")

            # Save message to DB
            db = next(get_db())
            msg = Message(group_id=group_id, sender_id=user_id, content=content)
            db.add(msg)
            db.commit()
            db.refresh(msg)

            sender = db.query(User).filter(User.id == user_id).first()
            db.close()

            # Broadcast to all in group
            await manager.broadcast(group_id, {
                "id": msg.id,
                "sender_name": sender.name if sender else "Unknown",
                "sender_id": user_id,
                "content": content,
                "timestamp": msg.timestamp.isoformat(),
            })
    except WebSocketDisconnect:
        manager.disconnect(ws, group_id)


# --- User Search & Direct Messages ---

@app.get("/search-users")
def search_users(q: str, db: Session = Depends(get_db)):
    if not q or len(q) < 2:
        return []
    users = db.query(User).filter(
        (User.name.ilike(f"%{q}%")) | (User.email.ilike(f"%{q}%"))
    ).limit(10).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "picture": u.picture} for u in users]


@app.get("/dm/{user1_id}/{user2_id}")
def get_dm_messages(user1_id: int, user2_id: int, db: Session = Depends(get_db)):
    msgs = (
        db.query(DirectMessage)
        .filter(
            ((DirectMessage.sender_id == user1_id) & (DirectMessage.receiver_id == user2_id)) |
            ((DirectMessage.sender_id == user2_id) & (DirectMessage.receiver_id == user1_id))
        )
        .order_by(DirectMessage.timestamp.asc())
        .all()
    )
    result = []
    for m in msgs:
        sender = db.query(User).filter(User.id == m.sender_id).first()
        result.append({
            "id": m.id,
            "sender_name": sender.name if sender else "Unknown",
            "sender_id": m.sender_id,
            "content": m.content,
            "timestamp": m.timestamp.isoformat(),
        })
    return result


# DM WebSocket connection manager
class DMConnectionManager:
    def __init__(self):
        # user_id -> list of websockets
        self.active: Dict[int, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user_id: int):
        await ws.accept()
        if user_id not in self.active:
            self.active[user_id] = []
        self.active[user_id].append(ws)

    def disconnect(self, ws: WebSocket, user_id: int):
        if user_id in self.active:
            self.active[user_id] = [w for w in self.active[user_id] if w != ws]

    async def send_to_user(self, user_id: int, message: dict):
        if user_id in self.active:
            data = json.dumps(message)
            for ws in self.active[user_id]:
                await ws.send_text(data)


dm_manager = DMConnectionManager()


@app.websocket("/ws/dm/{user_id}")
async def dm_websocket_endpoint(ws: WebSocket, user_id: int):
    await dm_manager.connect(ws, user_id)
    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            content = payload.get("content", "")
            receiver_id = payload.get("receiver_id")

            # Save DM to DB
            db = next(get_db())
            msg = DirectMessage(sender_id=user_id, receiver_id=receiver_id, content=content)
            db.add(msg)
            db.commit()
            db.refresh(msg)

            sender = db.query(User).filter(User.id == user_id).first()
            db.close()

            msg_data = {
                "id": msg.id,
                "sender_name": sender.name if sender else "Unknown",
                "sender_id": user_id,
                "content": content,
                "timestamp": msg.timestamp.isoformat(),
            }

            # Send to both sender and receiver
            await dm_manager.send_to_user(user_id, msg_data)
            await dm_manager.send_to_user(receiver_id, msg_data)
    except WebSocketDisconnect:
        dm_manager.disconnect(ws, user_id)
