""" Ugh, this is just for the priority queue """

class TupleSortingOn0(tuple):
    def __lt__(self, rhs):
        return self[0] < rhs[0]
    def __gt__(self, rhs):
        return self[0] > rhs[0]
    def __le__(self, rhs):
        return self[0] <= rhs[0]
    def __ge__(self, rhs):
        return self[0] >= rhs[0]
