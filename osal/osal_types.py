from enum import Enum

class OsalError( Enum ):
        OSAL_ERR_NONE = 0
        OSAL_ERR_FAILURE = 0xFF

class OsalEmitMessage( object ):
      Id : int
      Data : object
      def __init__(self, id : int, data : object):
            self.Id = id
            self.Data = data

class OsalMessage( object ):
      class MessageIds( Enum ):
            SET_EVENT = -1
            CLEAR_EVENT = -2
            SET_EVENT_TIMER = -3
            USER_MESSAGE_EVENT = -4

      def __init__(self, messageId : int, data : object = None):
            self.MessageId = messageId
            self.Data = data

      @classmethod
      def create_from_message(cls, message: 'OsalMessage') -> 'OsalMessage':
            """Factory method to create the correct message type based on MessageId.
            
            Args:
                message: The base OsalMessage to convert
                
            Returns:
                The correct message type instance
            """
            if message.MessageId == cls.MessageIds.SET_EVENT:
                  return OsalMessageSetEvent(message.EventId, message.Delay_seconds, message.Periodic)
            elif message.MessageId == cls.MessageIds.CLEAR_EVENT:
                  return OsalMessageClearEvent(message.EventId)
            elif message.MessageId == cls.MessageIds.SET_EVENT_TIMER:
                  return OsalMessageSetEventTimer(message.EventId, message.Delay_seconds, message.Periodic)
            elif message.MessageId == cls.MessageIds.USER_MESSAGE_EVENT:
                  return OsalMessageUserMessage(message.UserMessageId, message.Data)
            else:
                  return message

class OsalMessageUserMessage( OsalMessage ):
      """Message to notify OSAL process to handle a user message"""
      UserMessageId : int
      def __init__(self, userMessageId : int, data : object):
            super().__init__(OsalMessage.MessageIds.USER_MESSAGE_EVENT, data )
            self.UserMessageId = userMessageId

class OsalMessageSetEventTimer( OsalMessage ):
      """Message to notify OSAL process to set a delayed event from the timer process"""
      def __init__(self, eventId : int, delay_seconds : float = 0.0, periodic : bool = False):
            super().__init__(OsalMessage.MessageIds.SET_EVENT_TIMER)
            self.EventId = eventId
            self.Delay_seconds = delay_seconds
            self.Periodic = periodic

class OsalMessageSetEvent( OsalMessage ):
      """Message to notify OSAL process to set an event"""
      def __init__(self, eventId : int, delay_seconds : float = 0.0, periodic : bool = False):
            super().__init__(OsalMessage.MessageIds.SET_EVENT)
            self.EventId = eventId
            self.Delay_seconds = delay_seconds
            self.Periodic = periodic

class OsalMessageClearEvent( OsalMessage ):
      """Message to notify OSAL process to clear an event"""
      def __init__(self, eventId : int):
            super().__init__(OsalMessage.MessageIds.CLEAR_EVENT)
            self.EventId = eventId