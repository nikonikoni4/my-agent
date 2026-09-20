
class LoopControl:
    """
    该类是专门用于ReActAgent循环控制       

    loop control 信号目前只有continue和break，但是每种信号可能不只是控制循环,还需要改变循环种
    的某些状态，所以单独作为一个类进行管理   

    暂时不使用，现在先把大部分内容写在loop.py中，后续在慢慢分组件重构            
    """
    
    def hand_decision(self,error_type,decition:dict):

        pass