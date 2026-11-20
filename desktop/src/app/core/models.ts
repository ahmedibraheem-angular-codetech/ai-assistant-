// Shapes returned by the local API (see app/schemas/chat.py and app/conversations/schemas.py).

export interface ChatReply {
  model: string;
  answer: string;
  conversation_id?: string;
  agent_id?: string;
  agent_name?: string;
  memory_intent?: string;
  memory_action_id?: string;
  confirmation_required?: boolean;
  selected_agent_name?: string;
}

export interface Conversation {
  id: string;
  title: string;
  agent_id: string | null;
  created_at: string;
  updated_at: string;
  is_archived: boolean;
  message_count: number;
}

export interface ConversationList {
  conversations: Conversation[];
  count: number;
  limit: number;
  offset: number;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: string;
  content: string;
  agent_id: string | null;
  model: string | null;
  created_at: string;
  sequence_number: number;
}

export interface MessageList {
  messages: Message[];
  count: number;
  limit: number;
  offset: number;
}
